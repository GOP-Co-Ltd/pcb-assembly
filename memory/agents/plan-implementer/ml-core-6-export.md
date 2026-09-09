# MR6 `ml.export`（ONNX export / parity / static INT8 / model package）

計画書: `memory/agents/implementation-planner/ml-core-6-export.md`

実装フェーズでは `tests/**` を触っていない（`spec-test-author` と並列作業のため）。
末尾の「変異実験フェーズ」だけは `src/` と `tests/` の両方を担当した。

## 計画外の判断ログ

### 1. `torch.onnx.export(..., external_data=False)` を明示した（実測で判明した落とし穴）

torch 2.12 の `torch.onnx.export` は **`external_data: bool = True` が既定**で、
dynamo 経路では tiny model でも重みが `model.onnx.data` という sidecar へ出る。

計画書 §2 はこれに触れていない。planner の probe は export 先のディレクトリで
そのまま `onnx.load` したので、sidecar が隣にあり気付けなかった。

package へ `model.onnx` だけを copy すると `ImmutablePackage` の checksum は通るが、
`OnnxInferenceModel.load` が
`External data path does not exist: ".../model.onnx.data"` で落ちる。
manifest は model を 1 file (`model_filename`) として記録する設計なので、
sidecar を許すと成果物の同一性検査に穴が空く。

→ `external_data=False` を指定し、重みを ONNX ファイル内に収める。
2GB を超える model は export に失敗するが、その失敗は理由文字列で返る。
Pi へ載せる規模の model では起きない。

### 2. `OnnxExportOptions.as_dynamic_shapes()` が全 `input_names` の entry を返す

計画書 §4.7 の字面（宣言した軸だけの dict）では
`torch.export` が
`When dynamic_shapes is specified as a dict, its top-level keys must be the arg
names [...]` で落ちる。dict 形式の `dynamic_shapes` は **全引数名**を要求する。

→ 宣言の無い入力には空の dict を置く。戻り値型 `dict[str, dict[int, str]]` は
変えていない。`verify_dynamic_dimensions(expected=...)` へもこの写像をそのまま渡す
（空 entry は「その入力が graph に在る」ことだけを確認する）。

### 3. §4.7 の `expected_dynamic_dimensions()` に相当する公開メソッドを足さなかった

一度足しかけたが `as_dynamic_shapes()` と同一内容なので削除した。§4 に無い公開 API を
増やさない。

### 4. `InferenceManifest.validate()` で precision と quantization を結び付けていない（**裁定 2 で覆った**）

一度「`float32` に `quantization` を載せたら理由」「`static-int8` に無ければ理由」を
入れたが、§6.1 の異常系一覧にこの条件が無く、§6.1 のエッジ
「`quantization=None` と非 None の両方が往復する」を `attrs.evolve` で書かれると
衝突する。`PromotionDecision` 側の却下条件も
「`quantization` **があり** `calibration_split != "train"`」という書き方なので、
precision と quantization は独立に扱うのが計画書の意図と判断した。

同じ理由で `PromotionCandidate.validate()` も両者を結び付けていない。

### 5. `_reject_compiled_module` 相当の判定を `onnx_export.py` と `parity.py` に重複させた

`parity.py` は `RUNTIME_MODULES`（onnx 禁止）、`onnx_export.py` は onnx を引く層なので、
前者から後者を import できない。5 行の private helper を両方に置いた。
共有するには torch だけに依存する 9 個目の module が要り、§3 のファイル一覧から外れる。

### 6. `LatencyStatistics` の百分位は nearest-rank

補間（`numpy.percentile` 相当）ではなく `ceil(p/100 * n)` の順位で取る。
表 #49「percentile の index 計算を 1 ずらす」が index ベースの実装を前提にしており、
実機報告値に観測していない補間値を載せない方が妥当と判断した。

0.001〜0.100 の 100 点なら p50=0.050 / p95=0.095 / p99=0.099 になる。

### 7. `quantization.CalibrationSample.validate()` で dtype を検査していない

型注釈は §4.8 どおり `Mapping[str, NDArray[np.float32]]` にしたが、実行時に
`dtype == float32` を要求すると bool の mask 入力を持つ model の校正ができない。
入力名の食い違いは `OnnxGraphSummary.inspect(source)` の入力名と突き合わせて弾き、
dtype の食い違いは onnxruntime の例外を理由文字列にして返す。

### 8. `StaticQuantizationOptions.validate()` は `quantized_operator_types=()` を通す

§6.5 の異常系「全 op 指定にすると共有 initializer で失敗し、例外ではなく理由文字列が
返る」を成立させるため、空 tuple は `validate()` で弾かず `quantize_static` まで通す。
空の場合 onnxruntime は「全 operator」として扱う。

### 9. 型検査のための 3 箇所の逃げ

- `benchmark.py`: `np.savez(stream, **values)` は stub の `allow_pickle: bool` と
    keyword 名が衝突して pyright が落ちる。`cast("dict[str, Any]", ...)` で回避。
- `runtime.py`: `InferenceSession.run` の戻り値が広い union なので
    `cast("list[NDArray[np.float32]]", ...)`。
- `quantization.py`: `CalibrationDataReader.get_next` の基底注釈は `-> dict` だが、
    `__next__` は `None` を終端として扱う（`calibrate.py:204-208`）。
    実際の契約に合わせて `| None` を返し、
    `# pyright: ignore[reportIncompatibleMethodOverride]` を 1 行付けた。

### 10. `compile_parity.py` の `_tensor_difference` → `TensorDifference.between`

**「ついでのリファクタ」ではない。** `ml.export.parity` が同じ差分計算を再実装せずに
済ませるための公開化で、private 関数は他 module から import できないため必要。

`TensorDifference` は既に公開型なので `__all__` は変わらない。呼び出し側 3 箇所
（`measure` 内 2 箇所、`_compare_gradients` 1 箇所）を書き換えた。
`tests/ml/evaluation/test_compile_parity.py` は変更していない（緑のまま）。

### 11. `blocks.py` / `heads.py` の `int()` 除去にコメントを付けた

なぜ `int()` を書いてはいけないかが行だけでは伝わらないため、両方にコメントを添えた。

**（変異実験フェーズで文面を訂正）** 正確な機構は「非 strict export
（`torch.export.export` の `strict=False`。torch 2.12 の既定）が SymInt を example の値へ
落とす。`torch.onnx.export(..., dynamo=True)` は非 strict の失敗を黙って strict へ
フォールバックするので、ONNX の `dim_param` だけを見ても気付けない」。
詳細は計画書 §2.2 の訂正済みの表。

## 他 implementer への IF 変更通知

§4 のシグネチャ（引数名・keyword-only・戻り値型）は変えていない。

`spec-test-author` へ影響しうる差分は上記 2・4・8 の 3 点。特に:

- `OnnxExportOptions.as_dynamic_shapes()` の戻り値は宣言の無い入力も空 dict で含む
- ~~`InferenceManifest.validate()` は precision と quantization の組み合わせを見ない~~
    → **裁定 2 で覆り、`_validate_quantization` が結び付けるようになった**
- `StaticQuantizationOptions(quantized_operator_types=())` は `validate()` を通る

## 既知の制約・残課題

- **operator 許可 list は `ml` に無い。** 呼び出し側が
    `OnnxGraphSummary.verify_standard_operators(allowed_operator_types=...)` を
    呼び忘れられる（計画書 §9-3 のトレードオフのまま）。Phase 5 の release フローで
    必ず呼ぶこと。
- **`export` は 2GB 超の model を書き出せない**（判断 1 の代償）。失敗は理由文字列。
- **cold start の計測は `sys.executable` と環境変数に依存する。** 子プロセスは親の
    環境を継承する。`device_label` に計測条件を書き残す運用が要る。
- `tests/ml/test_architecture.py` の層登録は `spec-test-author` が実施済み。
    `DEPENDENCY_FREE_MODULES` へ `ml.export.manifest` / `ml.export.promotion`、
    `RUNTIME_MODULES` へ `ml.export.parity`、新設
    `INFERENCE_ONLY_MODULES` へ `ml.export.benchmark` / `ml.export.runtime`。
    実測では 3 層とも禁止依存ゼロを確認済み（下記）。
- **変異実験は実施済み。** 結果は末尾「変異実験フェーズ」。

## 検証結果

コンテナ内（`docker compose -f docker/compose.yaml exec -T ml`）。
`make ml-docker-check` は使っていない（pre-commit が並列 agent と `.git` で競合するため、
ruff / docformatter / mdformat / codespell を個別に実行した）。

- ruff check / ruff format --check: pass（対象 12 file）
- docformatter `--check --wrap-summaries=79 --wrap-descriptions=72`: pass（書き換えゼロ）
- mdformat `--number --check docs/...`: pass
- codespell: pass
- `pyright src/ml`: **0 errors, 0 warnings**
- `pytest tests/ml -m "not hardware and not e2e" --ignore=tests/ml/export`:
    **592 passed / 1 skipped**（main のベースラインと同じ）
- 層の実測（素の interpreter で import して `sys.modules` を確認）
    - `ml.export.manifest` + `ml.export.promotion`: 重い依存ゼロ
    - `ml.export.runtime` + `ml.export.benchmark`: `onnx` / `onnxscript` / `torch` /
        `torchvision` を 1 つも読まない
    - `ml.export.parity`: `onnx` / `onnxscript` を読まない
- 通し確認（scratchpad の使い捨てスクリプト。リポジトリには置いていない）
    - export → SSA 違反の拒否 → compile 済み wrapper の拒否 → 大きさ 1 の軸の拒否
        → batch dynamic の成功（`int()` 除去の効果）
    - parity（最大絶対差 6e-8、`passed=True`）
    - static INT8（`node_domains == ("",)`、既定は成功、`()` 指定は
        `Quantization parameter shared mode is not supported for weight yet` を
        理由文字列で返す）
    - manifest → `ImmutablePackage.publish` → `OnnxInferenceModel.load` → `predict`
    - 改竄検出 / manifest と実ファイル集合の不一致 / 未来の runtime 版の拒否 /
        足りない入力・違う dtype の理由化
    - `DeviceBenchmark.measure`（cold start 0.098 秒、peak RSS 68 MB。
        torch を読まない子プロセスで計測）
    - `PromotionDecision.decide` の全分岐（latency なし INT8 の却下を含む）

---

## 変異実験フェーズ

計画書 §7 の 62 行 + spec-test-author が実測で足した 4 行（A〜D）+ 実施中に足した行を、
1 件ずつ src へ当てて対象テストを回した。手順は scratchpad の driver
（pristine snapshot を sha256 で照合しながら復元）。**index は触っていない。**

### 裁定の反映

1. **裁定 1（`int()` 除去の根拠）。** 実測で planner と spec-test-author の主張を分離できた。

    | 宣言 | `int()` あり | `int()` なし |
    | --- | --- | --- |
    | `Dim.AUTO` + `strict=False`（torch 2.12 の既定） | example の値へ特殊化 | symbol のまま |
    | 名前付き `Dim` + `strict=False` | `UserError: Constraints violated` | OK |
    | `strict=True`（dynamo） | 特殊化しない | 特殊化しない |

    `torch.onnx.export(..., dynamo=True)` は非 strict の失敗を黙って strict へ落とすので
    ONNX の次元は変わらない。**`int()` を戻す変異は `TestExportedDynamicShapes` 4 本中
    3 本を落とす**（blocks の mask なし経路だけは落ちなくて正しい）。テストへ
    `strict=False` を明示し、torch の既定が変わっても検出力が消えないようにした。
    docs §6 / 計画書 §2.2・§4.10 / 本ノート #11 / spec-test-author のノートを訂正。

2. **裁定 2（manifest の precision ↔ quantization）。** `_validate_quantization` を足し、
    `static-int8` かつ `quantization=None`、`float32` かつ `quantization` 有りを理由で返す。
    `QUANTIZED_PRECISION` を公開定数にした。`PromotionCandidate` 側は変更なし。

3. **裁定 3（compile 済み判定）。** `onnx_export.py` / `parity.py` の
    `state_dict()` 前置き走査を `hasattr(model, "_orig_mod")` へ寄せた
    （`_COMPILED_WRAPPER_ATTRIBUTE`）。parameter を持たない compile 済み module も
    拒否できることをテストで固定した。

### 生き残った変異（4 件）

| # | 変異 | 生き残る理由 |
| --- | --- | --- |
| 29 | `export` から `verify_dynamic_dimensions` の呼び出しを削る | 現環境の `torch.export` は宣言できない軸を黙って特殊化せず例外を投げる。守っているのは `torch.onnx.export` の例外捕捉。機構そのものは #23 / #24 が `test_graph` で押さえている |
| 35 | `quant_pre_process` を `shutil.copyfile` へ置き換える | この tiny model では出力 graph に観測可能な差が出ない |
| 48 | session を `ORT_DISABLE_ALL` で作る | graph 最適化は定義上 数値を変えない。変異が意味的に no-op |
| 54 | warm-up loop を削る | 学習機の CPU では初回と 2 回目の差が計測ノイズに埋もれる。`warmup_count` の記録は往復テストが固定しているが、loop の実行そのものは mock なしに観測できない |

### 補強した変異（初回は生き残ったが、テストを足して潰した 9 件）

| # | 生き残った理由 | 補強 |
| --- | --- | --- |
| 6k | 空文字の軸は次の「10 進数か識別子か」検査にも掛かる | `test_names_an_empty_axis_as_empty` で理由まで見る |
| 11 | `candidate_id` の辞書順（`float32-candidate` < `static-int8-candidate`）が偶然 float32 を選んでいた | INT8 の id が前後する 2 ケースへ parametrize |
| 18 | SSA 違反は `full_check=False` でも落ちる | 素の checker は通り shape 推論だけが落ちる graph（broadcast 不能な `Add`）を足した |
| 19b | `is_file` が無くても `onnx.load` の例外捕捉が理由を返す | 不在の理由文字列を見る |
| 22 | 実 graph を作れていなかった | 既定 domain の node だけ + 呼ばれない local function を `onnx.helper` で組んだ。domain 検査と allowlist 検査が先に落ちない配置にしてある |
| 23 | 固定値 `1` は次の「symbol 名が違う」検査にも掛かる | 特殊化そのものを理由で見る |
| 25 | compile 済み wrapper は torch 側でも export に失敗するので、入口で拒否しなくても理由が返る | 理由が我々の拒否であることを見る + parameter を持たない compile 済み module のテスト |
| 31 | 3 本とも `_discard` へ到達しない失敗経路だった | 出力名 `mean` で SSA 違反を起こす（ファイルを書き終えてから検査が落ちる唯一の経路）テストを足した |
| 38 | source と量子化後で `node_domains` も dynamic 軸も同じ | QDQ node の存在を見る |

### 実測の記録

- 変異総数 102 件（round 1: 85、round 2 の再検証: 17）
- round 1 survivor 13 件 → 補強後 survivor 4 件（上表）
- 太字 18 行はすべて実測済み。#23 / #25 は初回 survivor で、補強後に落ちることを再実測した
- `tests/ml` = **783 passed / 1 skipped**（変異前 774 / 1）、pyright 0 errors

---

## レビュー対応ラウンド（code-reviewer の request-changes）

### 網羅変異 sweep（`src/ml/export/*.py` の全 if 文）

`ast` で `If` を列挙し、条件式を `False` へ置換して当該 module のテストを回した。
survivor は `tests/ml/export` 全体 + `test_architecture` へ広げて再確認している。
**バッチごとに sha256 で pristine 照合**（前後 2 回）。

| module | 分岐 | 初回 survivor | 補強後 survivor |
| --- | --- | --- | --- |
| manifest.py | 43 | 5 | 0 |
| promotion.py | 36 | 15 | 0 |
| graph.py | 9 | 0 | 0 |
| onnx_export.py | 21 | 5 | 1 |
| quantization.py | 17 | 6 | 1 |
| parity.py | 15 | 10 | 0 |
| runtime.py | 4 | 0 | 0 |
| benchmark.py | 31 → 30 | 23 | 2 |
| **計** | **176 → 175** | **64** | **4** |

初回 survivor の内訳はほぼ 2 種類だった。

1. **後段の分岐が前段を隠す**（M1 と同型）。例: `parity._validate_arguments` の
    `input_names` 空検査は、後続の `_verify_session_names` が同じ入力で理由を返すので
    消しても誰も気付かない。**理由文まで見るテストに直した。**
2. **object 単位のテストしか無く、分岐単位のケースが無い**（S4 と同型）。
    `AccuracyGate` / `LatencyGate` / `AccuracyEvidence` / `LatencyEvidence` /
    `PromotionCandidate` / `DeviceBenchmark` / `BenchmarkCase` の `validate()` が該当。
    **1 分岐 1 ケースの parametrize を足した。**

### 削除した分岐: `_measure_case` の warm-up loop の推論失敗チェック

warm-up 中の失敗は、直後の計測 loop が同じ入力で同じ理由を返すので、この分岐は
**冗長な防御**だった。変異で消しても区別が付かない（`:392` と `:399` が相互に隠し合う）。
S3（`_p95_seconds` の到達不能な `raise`）と MR5 の裁定 5 と同じ基準で削除した。

warm-up の戻り値は捨てる。握り潰しでないことは
`TestDeviceBenchmark::test_reports_a_case_the_model_cannot_run` が `warmup_count` 2 / 0 の
2 ケースで固定しており、計測 loop 側のチェックを潰すと**両方が落ちる**ことを実測した。

### 残る survivor 4 件（すべて到達不能か相互に区別不能）

| 位置 | 内容 | 判断 |
| --- | --- | --- |
| `onnx_export.py:222` | `export` 内の `verify_dynamic_dimensions(...)` 呼び出し | 既知（対応表 #29）。現環境の torch は宣言できない軸を黙って特殊化せず例外を投げるので、`export` の例外捕捉が先に理由を返す。機構自体は `test_graph` の #23 / #24 が単独で押さえている |
| `quantization.py:170` | 量子化後 `inspect` の失敗 | 量子化が成功した model が checker を通らない状況を作れない |
| `benchmark.py:273` | `OnnxInferenceModel.load` の失敗 | 親が読めない package は子プロセスでも読めず、cold start（:270）が先に落ちる |
| `benchmark.py:402` | `LatencyStatistics.of` の失敗 | `measured_count >= 1` かつ所要秒は有限正なので到達不能 |

残る 4 件はいずれも「消すと crash するだけの伝播 guard」か、torch / ORT が先に
拒否するために到達しない分岐で、テストで固定する価値より書くテストの技巧の方が
大きい。**現状維持で裁定済み。**

**レビュー 2 巡目で 3 件が survivor でなくなった。** いずれも「到達不能」の見立てが
誤りで、実測すると到達した。

- `benchmark:388`（`case.load_values()` の失敗）。cold start は `cases[0]` しか読まず、
    入口検査は `is_file()` しか見ないので、**2 件目**の壊れた `.npz` は計測 loop まで届く。
- `onnx_export:216`（opset 不一致）。**torch は古い opset を黙って引き上げる**
    （9 / 12 / 14 / 16 / 17 のいずれを頼んでも 18 が出る）。宣言と成果物が食い違う
    silent failure そのもので、この分岐はその唯一の検出器だった。
- `onnx_export:187`（`export` 内の `validate_for` 呼び出し）。**`torch.onnx.export` は
    大きさ 1 の example からでも dynamic な ONNX を書ける**（実測。`['batch', 6, 32, 32]`）。
    「torch が拒否するから到達しない」は誤りで、消すと export が通ってしまう。
    `size <= 1` を拒む理由は「その example では軸が変わることを確かめられない」であり、
    docstring をその理由づけへ直した。

### 裁定への対応

- **M1**: `_rejection_reason` の精度 gate 4 分岐（絶対 primary_score / 絶対 coverage /
    baseline 比 regression / baseline 比 coverage 差）をすべて理由文で固定し、baseline
    自身が絶対 gate に掛かる 2 ケースを足した。**変異実測で 4 分岐とも落ちる**。
- **S1**: `StaticQuantizationOptions.validate()` が `quantized_operator_types=()` を弾く。
    `quantize` の入口は `options.validate()` なので入口で弾かれる。**「quantize に
    別の検査を足す」のではなく既存の 1 か所へ寄せた**（規則が 2 か所に分かれるのを避けた）。
- **S2**: `validate_for` は未宣言の `input_name` を理由文字列で返す。`as_dynamic_shapes`
    は `setdefault` で例外を出さない。どちらも回帰テスト付き。
- **S3**: `_p95_seconds` ごと削除し、`_best_candidate` が `latency is not None` で絞る
    形にした。**`cast` は不要だった**（内包表記の絞り込みで pyright が narrowing する）。
- **S5**: `payload_filenames()` の重複除去を**削った**。`validate()` が
    manifest 名・model 名・extra の相互重複をすべて拒否するので、valid な manifest から
    到達できない。テストは valid な入力で整列だけを固定する形へ直した。
- **S8 / nit 1**: 記録側の訂正。計画書 §1 論点 4・§4.7 も `hasattr(_orig_mod)` へ同期。
- **nit 2**: `DEFAULT_MODEL_FILENAME` は `tests/ml/export/support.py` が import する形に
    した（計画 §4.1 の公開名を消さずに未使用を解消）。**`DEFAULT_DOMAIN` は未使用ではない**
    （`graph.py` 内で 2 箇所、テストでも使用）。
- **nit 3**: `_walk_nodes` の subgraph 再帰を `If` の分岐に node を隠した graph で固定。
- **nit 4**: `com.microsoft.nchwc` の決め打ちをやめた。機構は手組みの独自 domain graph で
    決定的に固定し、実 ORT の graph は「既定 domain 以外が出たら拒否される」ことだけを見て、
    独自 domain を出さない機体では skip する。

### 事故と手順

初回の sweep をバックグラウンドへ流してしまい、ターンを越えて走り続けた。
orchestrator が停止させるまでに 2 度、変異が当たったままの状態が観測された
（`parity.py` の 2 箇所。いずれも `git restore --worktree` で復元済み）。
**以後 sweep は同期・小バッチ・前後 sha256 照合で回した。** ドライバをターンを
越えて残さないこと。

### 結論: このコードベースは「先の分岐が後を隠す」構造を持ちやすい

**176 分岐を潰して初回 survivor 64 件（36%）** という数字が、その定量的な裏付けになった。
原因は 3 つとも `ml.export` 固有ではなく、このリポジトリの設計方針そのものに由来する。

1. **例外を投げず理由文字列で返す方針**（AGENTS.md / 計画 §8）は、検証を「最初に
    見つけた理由を返す」直列チェーンにする。**同じ不正入力が複数の分岐を発火させうる**
    ので、`error is not None` だけを見るテストは前段を消しても緑のままになる。
2. **3rd-party を最後の砦にしている経路**では、我々の入口検査を消しても
    onnxruntime / torch が同じ入力を拒否して理由が返る。`parity` の引数検証 10 分岐が
    まるごと空洞だったのはこれが理由。
3. **object 単位の parametrize**（「壊れた X を渡すと理由が返る」）は、その object の
    分岐数だけ穴を残す。`validate()` の分岐が 6 個あるのにケースが 1 個、という形。
4. **到達不能な伝播 guard が分岐として数えられる。** `X, error = f()` / `if X is None:`
    は `str | None` 規約が要求する定型で、`ast` の `If` 数え上げでは 1 分岐に見える。
    しかし前段が同じ失敗で先に落ちる場合は原理的に到達せず、必ず survivor になる。
    残った 4 件のうち 3 件がこれ。**次の sweep ではこの定型を先に除外して数える**と、
    survivor 率が「本当に守られていない検証」の指標になる。今回の 64 件も、この定型を
    除けば実質の空洞はもっと少ない。

**次の MR とドメイン層で使う対策:**

- `validate()` の異常系 parametrize は **1 分岐 1 ケース**にし、1 ケースにつき 1 分岐だけが
    発火する入力を選ぶ（例: `maximum_primary_score=0.0` は「0 以上の有限値」を通るので
    正値の検査だけが発火する）
- 検証チェーンのテストは `error is not None` で止めず、**理由文の識別できる部分**まで見る。
    これが前段・後段を区別する唯一の観測点になる
- **入口検査と 3rd-party の拒否が同じ入力で起きる箇所**は、理由文を見ないと必ず空洞化する。
    `ml` 側で弾いたのか ORT / torch が弾いたのかを assert する
- 到達不能な防御分岐は足さない。足してしまったら削る（S3 / `benchmark:392` / MR5 裁定 5）。
    **ただし「到達不能」と判定する前に実測すること。** 今回は `onnx_export` の 2 件と
    `benchmark` の 1 件が、実測すると到達した。torch / ORT の実装を根拠に「先に拒否される」
    と推論した箇所がことごとく外れている
- **削ってよいのは「内部状態が壊れていないこと」を確かめるだけの guard**
    （`_p95_seconds` の `raise`、warm-up loop の失敗チェック）。**成果物の契約を検査する
    分岐は、現在の 3rd-party 実装が先に拒否していても残す**（`export` の
    `verify_dynamic_dimensions` / opset 照合、`quantize` 後の `inspect`）。3rd-party の
    振る舞いが変わったときに silent failure になるのはそこだから。`str | None` 規約が
    要求する伝播 guard も削らない（消すと crash するだけで、規約から外れる）

### 申し送り: 理由文の部分一致 assert は日本語の文面にテストを結び付けている

上の対策で「理由文まで見る」を徹底した結果、**部分一致 assert が 30 箇所ほど増えた**。
今回は他に観測点が無い（`str | None` 規約では理由の種類を型で表せない）ので正しい選択
だが、文面を直すとテストが落ちる状態になっている。

ドメイン層で同じことをやると負債になる。次 MR 以降の候補として
**「理由コード（`Literal`）＋ 人間向け文面」の分離**がある。理由コードを assert すれば
文面の変更と検証の識別が独立する。`str | None` 規約そのものの変更を伴うので
MR6 の範囲外とし、記録だけ残す。
