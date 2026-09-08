# MR6 コア ML 基盤の最後の 1 本：`ml.export`

## 概要

学習済み model を ONNX へ出し、graph を検査し、static INT8 候補を作り、eager との
parity を測り、Raspberry Pi 5 で benchmark を取り、その全証拠から promote 可否を
決める層を `src/ml/export/` に追加する。装置ドメインは知らない。

作業ブランチは `feature/2026-09-08/ml-core-6-export`（`main` = 56f6fab から分岐）。
MR5（!205、`ml.config` / `ml.tuning`）は未 merge。MR6 はそれに依存しない。

検証は `make ml-docker-check`。ベースラインは **592 passed / 1 skipped**。
コンテナは onnx 1.22.0 / onnxruntime 1.29.0 / onnxscript 0.7.1 / torch 2.12.1。

**本計画の設計判断はすべてコンテナ内の実測に基づく。** 実測結果は §2 にまとめ、
各判断の根拠として参照する。

---

## 1. 論点 1〜4 の判断

### 論点 1: 層の分割

**判断: `ml/export/` を 8 module に割り、3 層＋新設 1 層に配置する。**

`docs/…-ml-plan.md` §7 の 1 行（「ONNX、quantization、parity、benchmark、runtime」）を
そのまま 1 module にすると Raspberry Pi 5 の推論経路が壊れる。分割の根拠は実測 §2.1：

- `import onnxruntime` は `onnx` を **読み込まない**（`sys.modules` に現れない）
- `import onnxruntime.quantization` は `onnx` を **読み込む**

つまり「ORT で推論する」経路と「量子化する」経路は依存が別物であり、同じ module に
置けない。

| module | 層 | import してよい重い依存 |
| --- | --- | --- |
| `ml/export/manifest.py` | `DEPENDENCY_FREE_MODULES` | なし（attrs / cattrs / stdlib） |
| `ml/export/promotion.py` | `DEPENDENCY_FREE_MODULES` | なし |
| `ml/export/runtime.py` | **`INFERENCE_ONLY_MODULES`**（新設） | `onnxruntime`、`numpy` |
| `ml/export/benchmark.py` | **`INFERENCE_ONLY_MODULES`**（新設） | `onnxruntime`、`numpy` |
| `ml/export/parity.py` | `RUNTIME_MODULES` | `torch`、`onnxruntime` |
| `ml/export/graph.py` | （層登録なし） | `onnx` |
| `ml/export/onnx_export.py` | （層登録なし） | `torch`、`onnx`、`onnxscript` |
| `ml/export/quantization.py` | （層登録なし） | `onnxruntime.quantization`（→ `onnx`） |

**`INFERENCE_ONLY_MODULES` を新設する理由。** `RUNTIME_MODULES` は torch を許す層だが、
Pi の実運転推論経路に torch を読ませたくない。§6 の gate は「process 起動から初回
予測までの cold latency」を含み、`import torch` はそこに数秒を直接足す。「Pi の推論
経路は onnxruntime と numpy だけで import できる」を機械検証する層を 1 つ足すのが
最も安い保証手段になる。

禁止依存は `INFERENCE_FORBIDDEN_DEPENDENCIES = ("onnx", "onnxscript", "torch",
"torchvision")`。`onnxruntime` は当然含めない。

**MR5 との競合を局所に留める措置**（重要）:

- **`HEAVY_DEPENDENCIES` と `TRAINING_ONLY_DEPENDENCIES` を一切変更しない。**
    `onnx` / `onnxscript` は既に両方に入っている。MR5 はこの 2 タプルから
    `hydra` / `omegaconf` を削るので、MR6 が触ると確実に衝突する
- `DEPENDENCY_FREE_MODULES` への追加は `ml.export.manifest` / `ml.export.promotion`。
    整列位置は `ml.experiment.provenance` の直後で、MR5 が触る位置
    （`ml.artifact.package` の直後と末尾）と隣接しない
- `RUNTIME_MODULES` への追加は `ml.export.parity` のみ。MR5 はこのタプルを触らない
- `INFERENCE_ONLY_MODULES` と `TestInferenceOnlyLayer` はファイル末尾に**追記**する
- **`pyproject.toml` と `uv.lock` を変更しない。** `ml-export` group は既に
    `onnx>=1.22,<2` と `onnxscript>=0.7,<1` を持ち、`ml-runtime` を include している。
    MR5 が最も大きく触るファイルを MR6 が触らずに済む

### 論点 2: Pi の benchmark をどこに置くか

**判断: `tests/ml` に hardware マーカーのテストを 1 件も置かない。**
**benchmark の「機構」は `src/ml/export/benchmark.py` に置き、その unit test は
hardware マーカー無しで `tests/ml/export/test_benchmark.py` に置く。実機での実行は
pytest ではなく運用ジョブとし、実機テストが要るなら Phase 5 で
`tests/pcbasm/pasting/paste_volume/` に置く。**

理由:

1. **`tests/ml` へ置くこと自体は技術的には可能。** `hardware` マーカーは
   `pyproject.toml` の `[tool.pytest.ini_options] markers` に登録済みで、
   `--strict-markers` 下でも `@pytest.mark.hardware` を直接書ける。
   `tests/helpers.mark_hardware` は `pytest.mark.hardware` の別名にすぎない
   （`tests/helpers.py:34`）ので、参照禁止は障害にならない
2. **しかし置く意味が無い。** Pi の benchmark に要るのは promote 済み model package
   ＝ 学習済み重み・前処理 schema・評価結果を含む**装置ドメインの成果物**であり、
   `ml` 単体では作れない。`tests/ml` に置くと fixture が装置ドメインを要求し、
   `test_architecture.py` の禁止（`tests/ml` は `pcbasm` を参照しない）に正面から当たる
3. **`make ml-docker-check` / `make ml-docker-test` は `-m "not hardware and not e2e"`
   で回る**（Makefile 参照）。`tests/ml` に hardware テストを置いても、学習機でも
   Pi でも一度も実行されない死んだテストになる
4. **Phase 4 の要求は「ユーザーが実機で実行できること」であって「pytest であること」
   ではない。** `DeviceBenchmark.measure()` を公開 API として出せば要求は満たせる。
   実行は Phase 5 の運用 CLI / WebUI ジョブから行う

代わりに MR6 が保証するのは次の 2 点で、どちらも学習機で回る通常テストになる:

- `LatencyStatistics.of()` の p50/p95/p99 が既知の並びに対して正しい
- `DeviceBenchmark.measure()` が実 ONNX model と実 ORT session を相手に
    cold latency・peak RSS・artifact size を実際に取れる（`tmp_path` の tiny model）

### 論点 3: INT8 の採否を誰がどう決めるか

**判断: MR5 の `StudyResults.verify_lineage()` と同型の「事後の純関数」にする。**
**`ml.export.promotion.PromotionDecision.decide()` が全候補の証拠を突き合わせ、
`LatencyEvidence`（実機実測）が無い候補を構造的に promote 不能にする。**

manifest のフィールドにしない理由: manifest は「この artifact は何か」を記録する
場所で、「なぜこれを選んだか」は候補**間**の比較なので単一 artifact に属さない。
`PromotionDecision` を独立 document（`DocumentKind(kind="ml-promotion-decision")`）
として保存し、manifest には結果としての `precision` と `QuantizationRecord` だけを
載せる。

`decide()` の判定順（`docs/…-ml-plan.md` §6「評価順序」6 と「promoted model package」
をそのまま関数にしたもの）:

1. 全候補の `validate()` を通す。`evaluated_split` が候補間で不一致なら理由を返す
   （候補選択中は validation、固定後は凍結 test。混ぜたら比較が成立しない）
2. `precision == "float32"` の baseline が無ければ理由を返す
3. 候補ごとに却下条件を評価する
   - `export_parity_passed` が偽
   - `primary_score > accuracy_gate.maximum_primary_score`（既定 0.10）
   - `abs(coverage - 0.683) > maximum_coverage_difference`（既定 0.03）
   - baseline 以外で `primary_score` の悪化が `maximum_primary_score_regression`
       （既定 0.01）を超える
   - baseline 以外で coverage 差が `maximum_coverage_difference` を超える
   - `quantization` があり `calibration_split != "train"`
   - **`latency is None`**（実機実測が無い）
   - `latency.worst_p95_seconds > latency_gate.maximum_p95_seconds`（既定 1.0）
4. 残った候補の p95 最小を選ぶ。最小との差が `negligible_difference_ratio`
   （既定 0.05）未満の候補が複数あれば、`artifact_bytes` 最小 → `float32` 優先 →
   `candidate_id` 昇順で決める
5. 残りが 0 なら `promoted_candidate_id=None` と、候補ごとの却下理由を返す

**「生成しただけで採用しない」がコードの構造そのものになる。** MR6 は INT8 を生成し
parity を測るところまでを行い、`LatencyEvidence` を作れるのは Pi で
`DeviceBenchmark.measure()` を回したユーザーだけなので、MR6 の成果物単体では
INT8 が promote されることが起こり得ない。

### 論点 4: eager model と compile 済み wrapper の境界

**判断: export の入口は `nn.Module` を 1 個受け取る。呼び出し側は
`TrainingTask.model` を渡す。`OnnxExportResult.export()` は
(a) compile 済み wrapper を理由文字列で拒否し、(b) `deepcopy` した複製を `eval()`
してから `torch.no_grad()` の中で export する。**

実測（§2.4）で分かったこと:

- `torch.compile(module).state_dict()` のキーは `_orig_mod.` 前置きになる。
    これは MR4 の `TrainingTask.model` docstring が「`state_dict()` のキーは
    checkpoint と export の公開契約」と書いている、まさにその契約違反の形
- **compile 済み wrapper を `torch.onnx.export` に渡しても例外にならない。**
    黙って通る。したがって「拒否する」は防御的な過剰処理ではなく、
    silent failure を検出する実機構になる
- **train mode のまま export しても UserWarning が出るだけで通る。** これも silent
    failure なので、入口で `eval()` を強制する
- `copy.deepcopy(model).eval()` は呼び出し側の `training` flag を変えない（実測確認）。
    `CompileParityResult.measure` が既に deepcopy している前例に揃う

**（裁定 3 で変更）** 拒否の判定は `hasattr(model, "_orig_mod")` とする。
当初は `any(key.startswith("_orig_mod.") for key in model.state_dict())` にしたが、
**parameter を 1 つも持たない module は `state_dict()` が空になり前置き判定を
すり抜ける**（実測）。`_orig_mod` は compile 済み wrapper が compile 前の module を
持つ属性で、parameter の有無に依らず現れる。`torch._dynamo.OptimizedModule` の
型そのものは private path なので参照しない。

---

## 2. 実測結果（設計の根拠）

すべてコンテナ内（`docker compose -f docker/compose.yaml exec -T ml python -`、
stdin 経由。リポジトリにファイルを書いていない）。

### 2.1 依存の実体

```
import onnxruntime              -> 'onnx' in sys.modules == False
import onnxruntime.quantization -> 'onnx' in sys.modules == True
```

### 2.2 非 strict export で dynamic 次元が黙って固定される

**（変異実験フェーズで訂正。下の表は「ONNX 入力 shape」ではなく
`torch.export.export` の placeholder shape として読むこと。）**

forward の中で軸に `int()` を掛けていると、**非 strict export（`torch.export.export` の
`strict=False`。torch 2.12 の既定）が SymInt を example 入力の値へ落とす**。

| 宣言 | `int()` あり | `int()` なし |
| --- | --- | --- |
| `Dim.AUTO` + `strict=False` | placeholder が `(2, 4)` へ特殊化（宣言が黙って無視される） | `(s3, 4)` |
| 名前付き `Dim` + `strict=False` | `UserError: Constraints violated (batch)!` | OK |
| `strict=True`（dynamo） | `(s27, 4)`。特殊化しない | `(s27, 4)` |

**`torch.onnx.export(..., dynamo=True)` は非 strict の失敗を黙って `strict=True` へ
フォールバックする**。だから最終的な ONNX の `dim_param` は `int()` の有無で変わらず、
ONNX だけを見ていると違反に気付けない。`int()` 除去は「将来の torch への保険」ではなく、
現に存在する制約違反の除去であり、silent fallback への依存をやめる変更である。

現行 `src/ml/model/` は両方の形を持つ:

- `heads.py` `_joined_inputs`: `int(conditioning.shape[0]) != int(features.shape[0])`
    → **conditioning を使う model は非 strict export で batch 次元が特殊化される**
- `blocks.py` `_reject_invalid_inputs`: mask 経路の
    `expected = (int(images.shape[0]), 1, int(images.shape[2]), int(images.shape[3]))`
    → **mask 付きの非 strict export では高さ・幅まで特殊化される**

`tests/ml/model/test_{blocks,heads}.py::TestExportedDynamicShapes` が
`torch.export.export(..., strict=False)` でこれを固定している。変異実験で
`int()` を戻すと 3/4 本が落ちることを実測済み。

`int()` を外しても eager の意味は変わらない（concrete tensor では `shape[i]` が
そのまま `int`）。`raise` 分岐の f-string 内の `int()` は条件が偽なら評価されないので
残してよい。

### 2.3 ONNX graph の実体（GroupNorm encoder + Gaussian head）

```
opset      [('', 20)]
ir_version 10   producer pytorch 2.12.1+cu130
node domains ['']         functions []
ops ['Add','Clip','Concat','Conv','Gemm','Greater','InstanceNormalization',
     'Mul','ReduceMean','Relu','Reshape','Shape','Softplus','Where']
```

**計画書 §6 の想定 operator 集合は実態と違う。** `GlobalAveragePool` は出ず
`ReduceMean` になる（`AdaptiveAvgPool2d((1,1))` の decomposition）。GroupNorm は
`InstanceNormalization` + `Reshape` + `Mul` + `Add` に分解される。`Greater` / `Where`
は `nn.Softplus` の threshold=20 分岐、`Clip` は log 分散の clamp。

→ **`ml` に operator 許可リストを持たせない。** 呼び出し側が
`verify_standard_operators(allowed_operator_types=...)` へ渡す。`ml` が保証するのは
「node domain が既定 domain だけで、custom function を含まない」ところまで。

### 2.4 出力名の衝突で `onnx.checker` が落ちる

`output_names=["mean", "log_variance"]` で export すると:

```
onnx.onnx_cpp2py_export.checker.ValidationError:
Graph must be in single static assignment (SSA) form,
however 'mean' has been used as output names multiple times.
```

graph 内部に `mean` という値名が既にあるため。`predicted_mean` /
`predicted_log_variance` にすると通る。**export の直後に
`onnx.checker.check_model(full_check=True)` を必ず通す**根拠。

### 2.5 static INT8 は既定設定では失敗する

```
quantize_static(..., quant_format=QDQ)   # op_types_to_quantize 未指定
-> ValueError: Quantization parameter shared mode is not supported for weight yet
   （WARNING:root: Axis 1 is out-of-range for weight 'val_14' with rank 1 を伴う）
```

原因は dynamo exporter の initializer 重複排除。同じ値の初期化子が複数 node に
共有される:

| 共有 initializer | shape | 参照 node |
| --- | --- | --- |
| `val_14` / `val_17` | `[8]` | `InstanceNormalization` × 7（GroupNorm の ones / zeros） |
| `val_27` / `val_29` | `[16,1,1]` | `Mul` / `Add` × 4（GroupNorm affine） |
| `val_10` | `[3]` | `Reshape` × 7 |

`op_types_to_quantize` を絞ると通る（実測）:

| 設定 | 結果 | FP32 に対する最大相対差 |
| --- | --- | --- |
| 既定（全 op） | **失敗** | — |
| `["Conv","Gemm"]`, `per_channel=True` | 成功 | 4.6e-3 |
| `["Conv","Gemm"]`, `per_channel=False` | 成功 | 5.0e-5 |
| `["Conv"]`, `per_channel=True` | 成功 | 1.4e-4 |

→ 既定を `quantized_operator_types=("Conv","Gemm")`、`per_channel=False` にする。
INT8 model の node domain は `['']` のみで、`onnx.checker(full_check=True)` を通る。

なお **INT8 の方がファイルサイズが大きい**ことがある（tiny model で 186 KB vs
154 KB）。QDQ node の追加分。`artifact_bytes` を promote の tie-break に使う設計は
これを前提にしている。

### 2.6 ORT の最適化済み FP32 を **ファイルとして出荷してはいけない**

`SessionOptions.optimized_model_filepath` で保存すると:

```
[W:onnxruntime] Serializing optimized model with Graph Optimization level greater
than ORT_ENABLE_EXTENDED and the NchwcTransformer enabled. The generated model may
contain hardware specific optimizations, and should only be used in the same
environment the model was optimized in.

domains ['', 'com.microsoft', 'com.microsoft.nchwc']
ops [..., 'FusedGemm', 'ReorderInput', 'ReorderOutput', ...]
```

計画書 §6 の「独自 operator や custom runtime extension は許可しない」に真正面から
反し、しかも学習機（x86）で最適化した graph を Pi（aarch64）へ持ち込むことになる。

→ **判断: 最適化済み FP32 を artifact 段階として持たない。** graph 最適化は
`OnnxInferenceModel.load()` が Pi 上で session を作るときに
`ORT_ENABLE_ALL` を指定して行う。計画書 §6 の 5 段 pipeline は
`best.pt → weights.pt → ONNX FP32 → static INT8 candidate → gate → promoted package`
の 4 段になる。docs 側もこの MR で同期する。

### 2.7 peak RSS を `resource.getrusage` から取ってはいけない

```
parent ru_maxrss (torch 読み込み後)  508800 KiB
child  ru_maxrss (execve 後)         508800 KiB   ← 親の high-water mark を継承
child  /proc/self/status VmHWM         9436 kB    ← 正しくリセットされる
```

→ peak RSS は `/proc/self/status` の `VmHWM` を読む。`ru_maxrss` は使わない。

### 2.8 その他の確認

- `dynamic_shapes` に `torch.export.Dim` ではなく素の文字列
    （`{"images": {0: "batch", 2: "height"}}`）を渡せる。API から
    `torch.export.Dim` を露出させずに済む
- `opset_version=18` / `20` はどちらも `dynamo=True` で効く
- 軸名の妥当性検査は無い。`"my batch"` がそのまま `dim_param` に入る
    → `validate()` で識別子形式を要求する
- FP32 ONNX と eager の最大絶対差は 32x32〜1024x64 の全 shape で **≤ 6e-8**

---

## 3. 追加・変更するファイル

```
src/ml/export/__init__.py            新規  docstring のみ。re-export しない
src/ml/export/manifest.py            新規  DEPENDENCY_FREE
src/ml/export/promotion.py           新規  DEPENDENCY_FREE
src/ml/export/runtime.py             新規  INFERENCE_ONLY（onnxruntime + numpy）
src/ml/export/benchmark.py           新規  INFERENCE_ONLY
src/ml/export/parity.py              新規  RUNTIME（torch + onnxruntime）
src/ml/export/graph.py               新規  onnx
src/ml/export/onnx_export.py         新規  torch + onnx + onnxscript
src/ml/export/quantization.py        新規  onnxruntime.quantization

src/ml/model/heads.py                変更  _joined_inputs の int() を外す（§2.2）
src/ml/model/blocks.py               変更  mask 検査の int() を外す（§2.2）
src/ml/evaluation/compile_parity.py  変更  _tensor_difference -> TensorDifference.between

docs/image-based-dispense-calibration-ml-plan.md  変更  §6 を実測へ同期（§2.3 / §2.6）

tests/ml/test_architecture.py        変更  層登録 3 箇所 + TestInferenceOnlyLayer 追記
tests/ml/export/__init__.py          新規
tests/ml/export/support.py           新規  6ch + conditioning の tiny model と export helper
tests/ml/export/test_manifest.py     新規
tests/ml/export/test_promotion.py    新規
tests/ml/export/test_graph.py        新規
tests/ml/export/test_onnx_export.py  新規
tests/ml/export/test_quantization.py 新規
tests/ml/export/test_parity.py       新規
tests/ml/export/test_runtime.py      新規
tests/ml/export/test_benchmark.py    新規
tests/ml/export/test_integration.py  新規  export -> parity -> quantize -> package -> load の通し
tests/ml/model/test_heads.py         変更  batch 次元が特殊化されないことを pin
tests/ml/model/test_blocks.py        変更  mask 経路で高さ・幅が特殊化されないことを pin
tests/ml/evaluation/test_compile_parity.py  変更なし想定（private 関数の移動のため）
```

**触らないもの（MR5 競合回避）**: `pyproject.toml`、`uv.lock`、`scripts/ml_smoke.py`、
`src/ml/config/`、`src/ml/tuning/`、`tests/ml/test_architecture.py` の
`HEAVY_DEPENDENCIES` / `TRAINING_ONLY_DEPENDENCIES`。

---

## 4. 公開インターフェース案（シグネチャ確定）

`spec-test-author` と `plan-implementer` を並列起動できる粒度まで確定させたもの。
**引数名・keyword-only の別・戻り値型を変えないこと。** 変える必要が出たら
orchestrator へ差し戻す。

全 attrs クラスは `@attrs.frozen`。Tensor / ndarray を持つものだけ `eq=False`。
検証は `validate() -> str | None`、失敗は `tuple[T | None, str | None]`。

### 4.1 `src/ml/export/manifest.py`（依存フリー層）

```python
INFERENCE_MANIFEST_DOCUMENT = DocumentKind(kind="ml-inference-manifest", schema_version=1)
MANIFEST_FILENAME = "manifest.json"
DEFAULT_MODEL_FILENAME = "model.onnx"

type Precision = Literal["float32", "static-int8"]


@attrs.frozen
class TensorContract:
    name: str
    element_type: str
    dimensions: tuple[str, ...]

    def validate(self) -> str | None: ...

    @property
    def dynamic_dimension_names(self) -> tuple[str, ...]: ...


@attrs.frozen
class QuantizationRecord:
    method: str
    activation_type: str
    weight_type: str
    per_channel: bool
    quantized_operator_types: tuple[str, ...]
    calibration_split: str
    calibration_sample_ids: tuple[str, ...]

    def validate(self) -> str | None: ...


@attrs.frozen
class InferenceManifest:
    model_filename: str
    precision: Precision
    opset_version: int
    onnx_ir_version: int
    inputs: tuple[TensorContract, ...]
    outputs: tuple[TensorContract, ...]
    minimum_onnxruntime_version: str
    exporter_versions: Mapping[str, str]
    training_run_id: str
    dataset_fingerprint: str
    split_fingerprint: str
    quantization: QuantizationRecord | None = None
    extra_payload_filenames: tuple[str, ...] = ()

    def validate(self) -> str | None: ...

    def payload_filenames(self) -> tuple[str, ...]: ...

    def verify_runtime(self, *, onnxruntime_version: str) -> str | None: ...

    def save(self, path: Path) -> None: ...

    @classmethod
    def load(cls, path: Path) -> tuple[InferenceManifest | None, str | None]: ...
```

- `dimensions` は `("1", "6", "height", "width")` のように固定値も symbol も文字列で
    持つ。数値と symbol を union にしない（strict converter で扱いづらく、JSON 表現も
    2 通りになる）
- `payload_filenames()` は `MANIFEST_FILENAME`、`model_filename`、
    `extra_payload_filenames` を整列した重複なし tuple。`ImmutablePackage.publish` の
    `payload_filenames` にそのまま渡せる形
- `verify_runtime` は dotted 数値の前置きだけを比較する。`packaging` は依存に無い。
    `validate()` が `minimum_onnxruntime_version` を `^\d+(\.\d+)*$` に限る
- `exporter_versions` は `_read_only_mapping`（`ml/experiment/provenance.py:50` と同型）
    で包む
- `extra_payload_filenames` にドメインの `preprocess.json` / `evaluation.json` が入る。
    `ml` はその中身を知らない

### 4.2 `src/ml/export/promotion.py`（依存フリー層）

```python
PROMOTION_DECISION_DOCUMENT = DocumentKind(kind="ml-promotion-decision", schema_version=1)
CALIBRATION_SPLIT = "train"


@attrs.frozen
class AccuracyGate:
    maximum_primary_score: float = 0.10
    reference_coverage: float = 0.683
    maximum_coverage_difference: float = 0.03
    maximum_primary_score_regression: float = 0.01

    def validate(self) -> str | None: ...


@attrs.frozen
class LatencyGate:
    maximum_p95_seconds: float = 1.0
    negligible_difference_ratio: float = 0.05

    def validate(self) -> str | None: ...


@attrs.frozen
class AccuracyEvidence:
    primary_score: float
    one_standard_deviation_coverage: float
    evaluated_split: str

    def validate(self) -> str | None: ...


@attrs.frozen
class LatencyEvidence:
    device_label: str
    worst_p95_seconds: float
    cold_start_seconds: float

    def validate(self) -> str | None: ...


@attrs.frozen
class PromotionCandidate:
    candidate_id: str
    precision: Precision
    artifact_bytes: int
    export_parity_passed: bool
    accuracy: AccuracyEvidence
    latency: LatencyEvidence | None = None
    quantization: QuantizationRecord | None = None

    def validate(self) -> str | None: ...


@attrs.frozen
class RejectedCandidate:
    candidate_id: str
    reason: str


@attrs.frozen
class PromotionDecision:
    promoted_candidate_id: str | None
    baseline_candidate_id: str
    evaluated_split: str
    rejected: tuple[RejectedCandidate, ...]
    summary: str

    @classmethod
    def decide(
        cls,
        candidates: Sequence[PromotionCandidate],
        *,
        accuracy_gate: AccuracyGate,
        latency_gate: LatencyGate,
    ) -> tuple[PromotionDecision | None, str | None]: ...

    def save(self, path: Path) -> None: ...

    @classmethod
    def load(cls, path: Path) -> tuple[PromotionDecision | None, str | None]: ...
```

`decide` の第 2 戻り値（理由文字列）は「判定そのものが成立しない」場合だけ。
候補が全部落ちた場合は `(PromotionDecision(promoted_candidate_id=None, ...), None)`
を返す。この 2 つを混ぜない。

判定不能の条件: 候補が空、いずれかの `validate()` が失敗、`candidate_id` が重複、
`evaluated_split` が候補間で不一致、`precision == "float32"` の候補が無い、
gate の `validate()` が失敗。

### 4.3 `src/ml/export/runtime.py`（推論専用層。torch を import しない）

```python
type InputValues = Mapping[str, NDArray[np.float32]]


class OnnxInferenceModel:
    @classmethod
    def load(
        cls,
        package: Path,
        *,
        onnxruntime_version: str | None = None,
    ) -> tuple[OnnxInferenceModel | None, str | None]: ...

    @property
    def manifest(self) -> InferenceManifest: ...

    @property
    def package_path(self) -> Path: ...

    @property
    def model_path(self) -> Path: ...

    def predict(self, inputs: InputValues) -> tuple[dict[str, NDArray[np.float32]] | None, str | None]: ...
```

- 唯一 `@attrs.frozen` でないクラス。ORT session という可変資源を持つため。
    属性は `_` prefix で持ち、上記 property だけ public にする
- `load` の順序: `ImmutablePackage.verify(package)` → `InferenceManifest.load` →
    manifest の `payload_filenames()` と実 checksum のキー集合の一致確認 →
    `verify_runtime` → `InferenceSession` 生成
- `onnxruntime_version=None` のときは `onnxruntime.__version__` を使う。引数で
    差し替えられるのは、Pi へ持ち込む前に学習機で拒否を確認できるようにするため
- session は `providers=["CPUExecutionProvider"]`、
    `graph_optimization_level=ORT_ENABLE_ALL`。§2.6 の判断により、最適化は
    **実行時にその機体で**行う
- `predict` は ORT の例外を捕まえて理由文字列にする。例外を漏らさない

### 4.4 `src/ml/export/benchmark.py`（推論専用層）

```python
DEVICE_BENCHMARK_DOCUMENT = DocumentKind(kind="ml-device-benchmark", schema_version=1)


@attrs.frozen
class LatencyStatistics:
    measured_count: int
    p50_seconds: float
    p95_seconds: float
    p99_seconds: float
    minimum_seconds: float
    maximum_seconds: float

    @classmethod
    def of(cls, durations: Sequence[float]) -> tuple[LatencyStatistics | None, str | None]: ...


@attrs.frozen(eq=False)
class BenchmarkCase:
    case_id: str
    input_path: Path

    def validate(self) -> str | None: ...

    @classmethod
    def write(
        cls,
        directory: Path,
        *,
        case_id: str,
        values: Mapping[str, NDArray[np.float32]],
    ) -> tuple[BenchmarkCase | None, str | None]: ...

    def load_values(self) -> tuple[dict[str, NDArray[np.float32]] | None, str | None]: ...


@attrs.frozen
class ColdStartMeasurement:
    elapsed_seconds: float
    peak_resident_kibibytes: int

    @classmethod
    def measure(
        cls,
        package: Path,
        case: BenchmarkCase,
    ) -> tuple[ColdStartMeasurement | None, str | None]: ...


@attrs.frozen
class CaseLatency:
    case_id: str
    statistics: LatencyStatistics


@attrs.frozen
class DeviceBenchmark:
    device_label: str
    onnxruntime_version: str
    warmup_count: int
    cases: tuple[CaseLatency, ...]
    cold_start_seconds: float
    peak_resident_kibibytes: int
    artifact_bytes: int

    @classmethod
    def measure(
        cls,
        package: Path,
        cases: Sequence[BenchmarkCase],
        *,
        device_label: str,
        warmup_count: int = 10,
        measured_count: int = 100,
    ) -> tuple[DeviceBenchmark | None, str | None]: ...

    @property
    def worst_p95_seconds(self) -> float: ...

    def as_latency_evidence(self) -> LatencyEvidence: ...

    def validate(self) -> str | None: ...

    def save(self, path: Path) -> None: ...

    @classmethod
    def load(cls, path: Path) -> tuple[DeviceBenchmark | None, str | None]: ...
```

- 入力を `.npz` ファイルで受けるのは、cold start の子プロセスへ渡すため。
    in-memory の ndarray を子へ渡す手段が無い
- `ColdStartMeasurement.measure` は
    `subprocess.run([sys.executable, "-c", _COLD_START_PROGRAM, str(package), str(npz)])`
    で子を起こす。子は `ml.export.runtime` と `numpy` だけを import し、
    `{"elapsed_seconds": ..., "peak_resident_kibibytes": ...}` を JSON で stdout へ出す。
    `_COLD_START_PROGRAM` は module 定数の文字列。
    `tests/ml/test_architecture.py:_loaded_dependencies` に同じ手法の前例がある
- peak RSS は子が `/proc/self/status` の `VmHWM` を読む（§2.7）。
    親の in-process 値は使わない
- `artifact_bytes` は package 配下の全 payload の合計バイト数
- `DeviceBenchmark.measure` は `cases[0]` で cold start を測り、全 case で
    warm loop を回す

### 4.5 `src/ml/export/parity.py`（runtime 層。onnx を import しない）

```python
@attrs.frozen(eq=False)
class ParityCase:
    case_id: str
    inputs: tuple[Tensor, ...]


@attrs.frozen
class CaseParity:
    case_id: str
    differences: tuple[TensorDifference, ...]
    non_finite_output_names: tuple[str, ...]
    non_positive_output_names: tuple[str, ...]

    @property
    def passed(self) -> bool: ...


@attrs.frozen
class OnnxParityResult:
    output_names: tuple[str, ...]
    cases: tuple[CaseParity, ...]

    @classmethod
    def measure(
        cls,
        model: nn.Module,
        onnx_model_path: Path,
        cases: Sequence[ParityCase],
        *,
        input_names: Sequence[str],
        output_names: Sequence[str],
        tolerance: ParityTolerance,
        positive_output_names: Sequence[str] = (),
    ) -> tuple[OnnxParityResult | None, str | None]: ...

    @property
    def passed(self) -> bool: ...
```

- `TensorDifference` と `ParityTolerance` は `ml.evaluation.compile_parity` の
    既存型をそのまま使う（§4.9 の refactor で `TensorDifference.between` を公開）
- `model` は `deepcopy(...).eval()` して `torch.no_grad()` で走らせる。
    compile 済み wrapper は `onnx_export` と同じ規則で拒否する
- `positive_output_names` は「mean が正」の要求（§6 evaluation 4）を
    ドメイン非依存に表したもの。`ml` は「µL」を知らない

### 4.6 `src/ml/export/graph.py`（onnx のみ）

```python
@attrs.frozen
class GraphTensor:
    name: str
    element_type: str
    dimensions: tuple[str, ...]

    def as_tensor_contract(self) -> TensorContract: ...


@attrs.frozen
class OnnxGraphSummary:
    ir_version: int
    producer: str
    opset_versions: Mapping[str, int]
    node_domains: tuple[str, ...]
    operator_types: tuple[str, ...]
    function_names: tuple[str, ...]
    inputs: tuple[GraphTensor, ...]
    outputs: tuple[GraphTensor, ...]

    @classmethod
    def inspect(cls, model_path: Path) -> tuple[OnnxGraphSummary | None, str | None]: ...

    def verify_standard_operators(
        self, *, allowed_operator_types: Set[str]
    ) -> str | None: ...

    def verify_dynamic_dimensions(
        self, *, expected: Mapping[str, Mapping[int, str]]
    ) -> str | None: ...

    @property
    def default_opset_version(self) -> int: ...
```

- `inspect` は `onnx.load` → `onnx.checker.check_model(full_check=True)` →
    要約。checker の `ValidationError` を理由文字列にする（§2.4 の SSA 違反はここで
    捕まる）
- `verify_standard_operators` は 3 つを同時に見る: node domain が `""` だけか、
    local function を持たないか、operator が `allowed_operator_types` に収まるか
- `verify_dynamic_dimensions` は「宣言した軸が `dim_param` を持ち、その名前が
    宣言と一致するか」を見る。固定値になっていたら理由を返す（§2.2 の検出器）

### 4.7 `src/ml/export/onnx_export.py`（torch + onnx + onnxscript）

```python
DEFAULT_OPSET_VERSION = 20


@attrs.frozen
class DynamicDimension:
    input_name: str
    axis: int
    symbol: str

    def validate(self) -> str | None: ...


@attrs.frozen
class OnnxExportOptions:
    input_names: tuple[str, ...]
    output_names: tuple[str, ...]
    dynamic_dimensions: tuple[DynamicDimension, ...] = ()
    opset_version: int = DEFAULT_OPSET_VERSION

    def validate(self) -> str | None: ...

    def validate_for(self, example_inputs: Sequence[Tensor]) -> str | None: ...

    def as_dynamic_shapes(self) -> dict[str, dict[int, str]]: ...


@attrs.frozen
class OnnxExportResult:
    model_path: Path
    summary: OnnxGraphSummary

    @classmethod
    def export(
        cls,
        model: nn.Module,
        example_inputs: Sequence[Tensor],
        model_path: Path,
        *,
        options: OnnxExportOptions,
    ) -> tuple[OnnxExportResult | None, str | None]: ...
```

`options.validate()` が拒否するもの:

- `input_names` / `output_names` が空、重複、空文字
- `input_names` と `output_names` の集合が交差する
- `symbol` が Python 識別子形式でない（§2.8）
- `input_name` が `input_names` に無い、`axis` が負

`options.validate_for(example_inputs)` が拒否するもの:

- `len(example_inputs) != len(input_names)`
- `axis` が example tensor の次元数を超える
- **`example_inputs` の該当軸の大きさが 1 以下**
    （`torch.export` が 0/1 次元を特殊化するため。§2.2）

`export` の手順と、その各段が返す理由:

1. compile 済み wrapper の拒否（`hasattr(model, "_orig_mod")`。裁定 3）
2. `options.validate()` / `options.validate_for(example_inputs)`
3. `copy.deepcopy(model).eval()`
4. `with torch.no_grad(): torch.onnx.export(..., dynamo=True, opset_version=...,
   dynamic_shapes=options.as_dynamic_shapes())`。例外は理由文字列にする
5. `OnnxGraphSummary.inspect(model_path)`（checker を含む）
6. `summary.verify_dynamic_dimensions(expected=...)`
7. `summary.default_opset_version != options.opset_version` なら理由

**operator 許可リストの検査は `export` に入れない。** 許可集合はドメインの判断
（§2.3）なので、呼び出し側が `result.summary.verify_standard_operators(...)` を呼ぶ。

### 4.8 `src/ml/export/quantization.py`（onnxruntime.quantization → onnx）

```python
DEFAULT_QUANTIZED_OPERATOR_TYPES = ("Conv", "Gemm")


@attrs.frozen(eq=False)
class CalibrationSample:
    sample_id: str
    values: Mapping[str, NDArray[np.float32]]

    def validate(self) -> str | None: ...


@attrs.frozen
class StaticQuantizationOptions:
    calibration_split: str = CALIBRATION_SPLIT
    per_channel: bool = False
    quantized_operator_types: tuple[str, ...] = DEFAULT_QUANTIZED_OPERATOR_TYPES
    minimum_calibration_samples: int = 8

    def validate(self) -> str | None: ...


@attrs.frozen
class StaticQuantizationResult:
    model_path: Path
    summary: OnnxGraphSummary
    record: QuantizationRecord

    @classmethod
    def quantize(
        cls,
        source_model_path: Path,
        model_path: Path,
        samples: Sequence[CalibrationSample],
        *,
        options: StaticQuantizationOptions,
    ) -> tuple[StaticQuantizationResult | None, str | None]: ...
```

- 内部で `quant_pre_process` → `quantize_static(quant_format=QuantFormat.QDQ,
  activation_type=QuantType.QUInt8, weight_type=QuantType.QInt8,
  op_types_to_quantize=list(options.quantized_operator_types), ...)` を呼ぶ
- `CalibrationDataReader` の実装は private class `_SampleReader`。
    3rd-party の ABC を実装するのは API 要件であってモックではない。`get_next` に
    `@override` を付ける（pyright `reportImplicitOverride`）
- `quantize` は `len(samples) < options.minimum_calibration_samples` を拒否する
- `record.calibration_sample_ids` は与えられた `sample_id` を整列して入れる。
    ドメインが「train split だけを使った」ことを事後に検証できるようにする
- `quantize` は最後に `OnnxGraphSummary.inspect`（checker を含む）を通す

### 4.9 `src/ml/evaluation/compile_parity.py` の変更

module-level の `_tensor_difference(eager, compiled, tolerance)` を
`TensorDifference.between(left, right, *, tolerance)` の classmethod へ移す。
`__all__` は変わらない（`TensorDifference` は既に公開）。

理由: AGENTS.md の「クラスに属する関数は module-level に置かず classmethod にする」
に合致し、`ml.export.parity` が同じ差分計算を再実装せずに済む。
呼び出し側の 3 箇所（`measure` 内 2 箇所、`_compare_gradients` 1 箇所）を書き換える。
`tests/ml/evaluation/test_compile_parity.py` は private 関数を見ていないので変更不要。

### 4.10 `src/ml/model/heads.py` / `blocks.py` の変更

```python
# heads.py _joined_inputs（現在 int() を掛けている 1 箇所）
if conditioning.shape[0] != features.shape[0]:

# blocks.py _reject_invalid_inputs（mask 経路）
if valid_pixel_mask.shape != (images.shape[0], 1, images.shape[2], images.shape[3]):
```

eager では `Tensor.shape[i]` が `int` なので意味は変わらない。`raise` 側の f-string は
現状のままでよい（条件が偽なら評価されない）。`torch.Size` 同士の比較になる場合は
`tuple(...)` を挟まないこと。`int()` を挟むと特殊化が戻る。

**（変異実験フェーズで訂正）** この 2 箇所を落としても、`torch.onnx.export` が
非 strict の失敗を黙って strict へフォールバックするため**出荷 ONNX の次元は変わらない**。
それでも外すのは、`int()` が非 strict export の制約に現に違反しており、
成果物が silent fallback に依存する状態をやめるため。`torch.export.export` を直接使う
経路（`ml.evaluation` の compile parity、将来の ExecuTorch 等）では宣言が黙って無視される。

---

## 5. 実装ステップと commit 境界

依存順。各 commit で `git add -A && make ml-docker-check` を 2 回通す。

### commit 1 — `refactor(ml): tensor 差分の生成を classmethod へ寄せる`

`_tensor_difference` → `TensorDifference.between`。§4.9。
既存テストが緑のままであることを確認する（振る舞いを変えない）。

### commit 2 — `fix(ml): 動的次元を固定してしまう shape 検査を直す`

§4.10 の 2 箇所 + `tests/ml/model/test_heads.py` / `test_blocks.py` へ
「`torch.export.export` 後の入力 shape に `dim_param` が残る」テストを足す。
テスト側は `torch.export.export` を直接使い、onnx を import しない
（`tests/ml/model/` を export 依存にしない）。

### commit 3 — `feat(ml): 推論成果物の manifest を追加する`

`src/ml/export/__init__.py`、`manifest.py`、`tests/ml/export/test_manifest.py`。
`tests/ml/test_architecture.py` の `DEPENDENCY_FREE_MODULES` へ
`"ml.export.manifest"` を追加。

### commit 4 — `feat(ml): ONNX graph の検査を追加する`

`graph.py`、`tests/ml/export/test_graph.py`、`tests/ml/export/support.py`。
support には 6 channel + conditioning の tiny model と、その FP32 ONNX を
`tmp_path` へ書く helper を置く。

### commit 5 — `feat(ml): eager model の ONNX export を追加する`

`onnx_export.py`、`tests/ml/export/test_onnx_export.py`。

### commit 6 — `feat(ml): ONNX 出力と eager の parity 検証を追加する`

`parity.py`、`tests/ml/export/test_parity.py`。
`RUNTIME_MODULES` へ `"ml.export.parity"` を追加。

### commit 7 — `feat(ml): 静的 INT8 量子化を追加する`

`quantization.py`、`tests/ml/export/test_quantization.py`。

### commit 8 — `feat(ml): 推論 package の読み込みと実行を追加する`

`runtime.py`、`tests/ml/export/test_runtime.py`。
`test_architecture.py` へ `INFERENCE_ONLY_MODULES` /
`INFERENCE_FORBIDDEN_DEPENDENCIES` / `TestInferenceOnlyLayer` を**末尾に追記**。

### commit 9 — `feat(ml): 実機 latency の計測を追加する`

`benchmark.py`、`tests/ml/export/test_benchmark.py`。
`INFERENCE_ONLY_MODULES` へ `"ml.export.benchmark"` を追加。

### commit 10 — `feat(ml): promote 可否を判定する純関数を追加する`

`promotion.py`、`tests/ml/export/test_promotion.py`。
`DEPENDENCY_FREE_MODULES` へ `"ml.export.promotion"` を追加。

### commit 11 — `test(ml): export から package 読み込みまでの通しを pin する`

`tests/ml/export/test_integration.py`。

### commit 12 — `docs(ml): export 計画を実測へ同期する`

`docs/image-based-dispense-calibration-ml-plan.md` §6 を §2.3 / §2.6 / §2.5 の実測へ
合わせる。**創作しない。** 直す箇所:

- operator 集合（`GlobalAveragePool` → `ReduceMean`、GroupNorm の
    `InstanceNormalization` 分解）
- 「ONNX Runtime optimized FP32」を artifact 段階から外し、実行時 graph 最適化に
    置き換える。理由（`com.microsoft.nchwc` 混入と機体固定）を書く
- static INT8 が既定設定では失敗し `op_types_to_quantize` を絞る必要があること
- batch を dynamic にするかの判断を Phase 5 のドメイン側へ委ねること

commit 3〜10 は 4.x の interface が確定しているので、`spec-test-author` が
テストを、`plan-implementer` が実装を**並列**で進められる。commit 1・2 は先に
単独で通す（既存 module を触るため）。

---

## 6. テスト観点

区分はすべて integration-with-fakes 相当（実 onnx / 実 ORT / 実ファイルを使う）か
unit。**モックは書かない。** ONNX model は `tmp_path` へ実際に書き、ORT session を
実際に作って走らせる。

### 6.1 `test_manifest.py`

- 正常系: save → load の往復で全フィールドが一致する
- 正常系: `payload_filenames()` が manifest / model / extra を整列した重複なし tuple を返す
- 正常系: `verify_runtime` が同版・上位版で `None`、下位版で理由
- 異常系: 未知 `kind` / 別 `schema_version` の JSON を `load` すると理由
- 異常系: `validate()` — 空の `model_filename`、`extra_payload_filenames` に
    `manifest.json` や `SHA256SUMS` が混ざる、`minimum_onnxruntime_version` が非数値、
    `inputs` が空、`TensorContract.dimensions` に空文字
- エッジ: `quantization=None` と非 None の両方が往復する
- エッジ: `minimum_onnxruntime_version="1.29"` と `"1.29.0"` の比較が成立する

### 6.2 `test_promotion.py`

- 正常系: FP32 のみ・latency 有り → FP32 が promote される
- 正常系: INT8 が p95 で明確に速い（差が 5% 超）→ INT8 が promote される
- 正常系: p95 差が 5% 未満 → `artifact_bytes` の小さい方
- 正常系: p95 差 5% 未満で `artifact_bytes` も同じ → `float32` が勝つ
- **異常系: INT8 に `latency=None` → INT8 は却下、FP32 が promote される**（論点 3 の核）
- 異常系: 全候補 `latency=None` → `promoted_candidate_id is None` と却下理由
- 異常系: `primary_score` が gate 超 → 却下
- 異常系: coverage 差が gate 超 → 却下
- 異常系: baseline に対する `primary_score` 悪化が 0.01 超 → INT8 だけ却下
- 異常系: `quantization.calibration_split == "validation"` → 却下
- 異常系: `export_parity_passed=False` → 却下
- 異常系: p95 が 1.0 秒超 → 却下
- 判定不能: 候補が空 / `candidate_id` 重複 / `evaluated_split` 不一致 /
    FP32 候補が無い → 第 2 戻り値に理由
- 正常系: `save` → `load` の往復

### 6.3 `test_graph.py`

- 正常系: 実 export した FP32 model を `inspect` すると
    `node_domains == ("",)`、`function_names == ()`、`opset_versions == {"": 20}`
- 正常系: `verify_dynamic_dimensions` が宣言どおりの symbol で `None`
- 異常系: `inspect` が存在しないパス / 壊れたバイト列 / JSON で理由
- **異常系: 出力名を graph 内部の値名（`mean`）に衝突させた model で
    `inspect` が checker の SSA 違反を理由として返す**（§2.4 の実測を pin）
- 異常系: `verify_standard_operators` が許可外 operator を列挙する
- 異常系: `verify_dynamic_dimensions` が固定値になった軸を理由にする
- 異常系: `verify_dynamic_dimensions` が symbol 名の食い違いを理由にする

### 6.4 `test_onnx_export.py`

- 正常系: 高さ・幅 dynamic の export が通り、`summary` の入力 shape が
    `("1", "6", "height", "width")` になる
- 正常系: `opset_version=18` を指定すると `default_opset_version == 18`
- 正常系: 呼び出し側の model の `training` flag と重みが変わらない
- **異常系: `torch.compile` した module を渡すと理由を返し、ファイルを作らない**（論点 4）
- **異常系: 大きさ 1 の軸へ `DynamicDimension` を宣言すると
    `validate_for` が理由を返す**（§2.2）
- **異常系: `int()` ガードのある model で batch を dynamic 宣言すると
    export 後の `verify_dynamic_dimensions` が理由を返す**（§2.2 の検出器）
- 異常系: `options.validate()` — 重複名、入出力名の交差、非識別子の symbol、
    `input_names` に無い `input_name`、負の `axis`
- 異常系: `example_inputs` の個数が `input_names` と合わない
- エッジ: `dynamic_dimensions=()` の完全固定 shape export が通る

### 6.5 `test_quantization.py`

- **正常系: GroupNorm を含む実 model を `quantize` でき、`summary.node_domains`
    が `("",)` のまま**（§2.5 の回避策が効いていること）
- 正常系: `record` に `quantized_operator_types` / `per_channel` /
    `calibration_split` / 整列済み `calibration_sample_ids` が入る
- 正常系: 生成した INT8 model を実 ORT で走らせ、FP32 との相対差が緩い閾値内
- **異常系: `quantized_operator_types=()`（＝全 op 量子化）にすると
    共有 initializer で失敗し、例外ではなく理由文字列が返る**（§2.5）
- 異常系: calibration sample が `minimum_calibration_samples` 未満 → 理由
- 異常系: 入力名が model と食い違う calibration sample → 理由
- 異常系: `source_model_path` が存在しない → 理由

### 6.6 `test_parity.py`

- 正常系: FP32 ONNX と eager の差が `ParityTolerance(1e-4, 1e-6)` 内で `passed`
- 正常系: 最小・最大・縦長・横長の 4 shape を個別 case として通す
- 正常系: `positive_output_names` を指定して、正の出力が
    `non_positive_output_names` に現れない
- 異常系: わざと重みをずらした model と比べると `passed` が偽で、
    `differences` に食い違いが載る
- 異常系: `positive_output_names` に負値を返す出力を指定すると列挙される
- 異常系: compile 済み wrapper を渡すと理由
- 異常系: `input_names` が session の入力と合わない → 理由
- エッジ: `cases` が空 → 理由

### 6.7 `test_runtime.py`

- 正常系: 実 package を publish → `load` → `predict` が eager と一致する
- 正常系: `manifest` / `package_path` / `model_path` が読める
- 異常系: `SHA256SUMS` の 1 行を書き換えると `load` が理由（`ImmutablePackage` 経由）
- **異常系: manifest の `extra_payload_filenames` に package に無い名前を入れると
    `load` が理由**（manifest と実ファイル集合の一致検査）
- 異常系: `minimum_onnxruntime_version` を未来版にすると `load` が理由
- 異常系: `predict` に足りない入力名 / 違う dtype を渡すと例外ではなく理由
- 異常系: package ディレクトリが無い → 理由

### 6.8 `test_benchmark.py`

- 正常系: `LatencyStatistics.of` が既知の並び（例: 0.001〜0.100 の 100 点）に対して
    p50 / p95 / p99 / min / max を返す
- 異常系: `LatencyStatistics.of(())` が理由
- 異常系: 非有限・負の duration が理由
- 正常系: `BenchmarkCase.write` → `load_values` の往復
- **正常系: `ColdStartMeasurement.measure` が実 package に対して
    `elapsed_seconds > 0` と `peak_resident_kibibytes > 0` を返し、
    その RSS が親プロセスの `ru_maxrss` より小さい**（§2.7 の pin）
- 正常系: `DeviceBenchmark.measure` が全 case の `CaseLatency` を返し、
    `worst_p95_seconds` が最大の p95 と一致する
- 正常系: `as_latency_evidence()` が `promotion.LatencyEvidence` を返す
- 正常系: `save` → `load` の往復
- 異常系: `warmup_count < 0` / `measured_count < 1` / `cases` が空 → 理由
- 異常系: 壊れた package → 理由（例外を漏らさない）

テスト時間を抑えるため `measured_count` は 5〜20 程度で呼ぶ。既定値 100 は
既定値であることだけを確認する。

### 6.9 `test_integration.py`

- eager model → FP32 export → parity → INT8 quantize → manifest →
    `ImmutablePackage.publish` → `OnnxInferenceModel.load` → `predict` の通し
- 同じ通しを FP32 / INT8 の 2 候補で行い、`PromotionDecision.decide` へ
    `LatencyEvidence` 付きで渡して FP32 が選ばれること
- **INT8 側の `latency` を落とすと INT8 が却下されること**

### 6.10 `test_architecture.py`（変更）

- `DEPENDENCY_FREE_MODULES` に `ml.export.manifest` / `ml.export.promotion`
- `RUNTIME_MODULES` に `ml.export.parity`
- `INFERENCE_ONLY_MODULES = ("ml.export.benchmark", "ml.export.runtime")` と
    `INFERENCE_FORBIDDEN_DEPENDENCIES = ("onnx", "onnxscript", "torch", "torchvision")`
    を末尾へ追記し、`TestInferenceOnlyLayer` を追加する

---

## 7. テスト ↔ 潰す機構の対応表

**実装フェーズでこれを実測すること。** 各行の「潰す操作」を施して、対応するテストが
**実際に落ちる**ことを確認し、確認後に必ず戻す。

**変異実験の安全手順**（MR5 の事故を繰り返さない）:
scratchpad へ `git diff` の snapshot と対象ファイルの sha256 を取り、
戻したあと sha256 の一致を確認する。`git restore` に頼らない。
**変異実験の最中は orchestrator が index を触らない。**

| # | テスト | 潰す機構（この操作でそのテストが落ちる） |
| --- | --- | --- |
| 1 | `test_manifest::TestPayloadFilenames::…manifestを含む` | `payload_filenames` から `MANIFEST_FILENAME` を落とす |
| 2 | `test_manifest::TestPayloadFilenames::…重複を潰す` | `sorted(set(...))` を `sorted(...)` にする |
| 3 | `test_manifest::TestVerifyRuntime::…下位版で理由` | `verify_runtime` を常に `None` にする |
| 4 | `test_manifest::TestVerifyRuntime::…1.29と1.29.0` | 版比較を文字列比較にする |
| 5 | `test_manifest::TestLoad::…schema_version不一致` | `INFERENCE_MANIFEST_DOCUMENT.schema_version` を上げて `load` 側だけ据え置く |
| 6 | `test_manifest::TestValidate::各異常系` | `validate()` の該当分岐を 1 つずつ削る（parametrize の該当ケースだけ落ちる） |
| 7 | **`test_promotion::TestDecide::…latencyなしのINT8を却下`** | `decide` の `if candidate.latency is None` 分岐を削る（**論点 3 の核。ここが空洞化すると Pi 実測なしで INT8 が promote される**） |
| 8 | **`test_promotion::TestDecide::…全候補latencyなし`** | 同上 |
| 9 | `test_promotion::TestDecide::…p95最小を選ぶ` | 選択を `candidates[0]` 固定にする |
| 10 | `test_promotion::TestDecide::…差5%未満はartifact小` | `negligible_difference_ratio` の分岐を削り常に p95 最小を採る |
| 11 | `test_promotion::TestDecide::…同点はfloat32優先` | tie-break から `precision == "float32"` を落とす |
| 12 | `test_promotion::TestDecide::…baseline悪化0.01超で却下` | baseline 比較の分岐を削る |
| 13 | **`test_promotion::TestDecide::…calibration_splitがtrain以外で却下`** | `CALIBRATION_SPLIT` 比較を削る（「calibration には train sample だけ」の唯一の機械保証） |
| 14 | `test_promotion::TestDecide::…evaluated_split不一致で判定不能` | 一致検査を削る |
| 15 | `test_promotion::TestDecide::…float32候補なしで判定不能` | baseline 探索の失敗分岐を削る |
| 16 | `test_promotion::TestDecide::…p95が1秒超で却下` | `maximum_p95_seconds` の分岐を削る |
| 17 | `test_promotion::TestDecide::…parity失敗で却下` | `export_parity_passed` の分岐を削る |
| 18 | **`test_graph::TestInspect::…出力名衝突でSSA違反`** | `inspect` の `onnx.checker.check_model(..., full_check=True)` を `full_check=False` にする／呼ばない |
| 19 | `test_graph::TestInspect::…壊れたファイルで理由` | `onnx.load` の例外捕捉を外す |
| 20 | `test_graph::TestVerifyStandardOperators::…許可外を列挙` | 許可リスト比較を削る |
| 21 | `test_graph::TestVerifyStandardOperators::…custom domainを拒否` | `node_domains` の検査を削る |
| 22 | `test_graph::TestVerifyStandardOperators::…local functionを拒否` | `function_names` の検査を削る |
| 23 | **`test_graph::TestVerifyDynamicDimensions::…固定値を理由にする`** | `dim_param` が空のときも通すようにする |
| 24 | `test_graph::TestVerifyDynamicDimensions::…symbol名の食い違い` | 名前一致の比較を削り存在確認だけにする |
| 25 | **`test_onnx_export::TestExport::…compile済みwrapperを拒否`** | `_orig_mod.` 前置きの検査を削る（**論点 4。実測で「黙って通る」ことを確認済み**） |
| 26 | **`test_onnx_export::TestExport::…呼び出し側のtrainingが変わらない`** | `copy.deepcopy` を外して `model.eval()` を直接呼ぶ |
| 27 | `test_onnx_export::TestExport::…evalで書き出す` | `.eval()` を削る（train mode の GroupNorm 差ではなく、`summary` に残る差で検出する。検出が弱ければ #26 に統合してよい） |
| 28 | **`test_onnx_export::TestValidateFor::…大きさ1の軸を拒否`** | `size <= 1` の検査を削る |
| 29 | **`test_onnx_export::TestExport::…特殊化された軸で理由`** | `export` から `verify_dynamic_dimensions` の呼び出しを削る |
| 30 | `test_onnx_export::TestExport::…opset_versionが効く` | `torch.onnx.export` へ `opset_version` を渡すのを止める |
| 31 | `test_onnx_export::TestExport::…失敗時にファイルを作らない` | 失敗経路で `model_path.unlink(missing_ok=True)` を止める |
| 32 | `test_onnx_export::TestOptions::各異常系` | `validate()` の該当分岐を 1 つずつ削る |
| 33 | **`test_quantization::TestQuantize::…GroupNorm modelを量子化できる`** | `op_types_to_quantize` に `options.quantized_operator_types` を渡すのを止める（§2.5 の失敗が再現する） |
| 34 | **`test_quantization::TestQuantize::…全op指定で理由を返す`** | `quantize_static` の例外捕捉を外す（`ValueError` が漏れる） |
| 35 | `test_quantization::TestQuantize::…quant_pre_processを通す` | `quant_pre_process` の呼び出しを削る |
| 36 | `test_quantization::TestQuantize::…sample_idsが整列` | `sorted(...)` を外す |
| 37 | `test_quantization::TestQuantize::…最小件数未満で理由` | `minimum_calibration_samples` の検査を削る |
| 38 | `test_quantization::TestQuantize::…node domainが空のまま` | `OnnxGraphSummary.inspect` を呼ばず `summary` を捏造する |
| 39 | `test_parity::TestMeasure::…4 shapeを個別に通す` | `cases` の走査を `cases[:1]` にする |
| 40 | `test_parity::TestMeasure::…重みをずらすとpassedが偽` | `passed` を常に `True` にする |
| 41 | **`test_parity::TestMeasure::…非有限出力を列挙`** | `non_finite_output_names` の収集を削る |
| 42 | **`test_parity::TestMeasure::…正でない出力を列挙`** | `positive_output_names` の判定を削る |
| 43 | `test_parity::TestMeasure::…compile済みwrapperを拒否` | 検査を削る |
| 44 | `test_runtime::TestLoad::…改竄を検出` | `ImmutablePackage.verify` の呼び出しを削る |
| 45 | **`test_runtime::TestLoad::…manifestと実ファイル集合の不一致`** | `payload_filenames()` と `verified.checksums` のキー集合比較を削る |
| 46 | `test_runtime::TestLoad::…未来のruntime版を拒否` | `verify_runtime` の呼び出しを削る |
| 47 | `test_runtime::TestPredict::…足りない入力で理由` | ORT の例外捕捉を外す |
| 48 | `test_runtime::TestPredict::…eagerと一致` | session を `ORT_DISABLE_ALL` で作る（一致は崩れないので、崩れないことを確認したうえで #49 に寄せてよい） |
| 49 | `test_benchmark::TestLatencyStatistics::…p95が正しい` | percentile の index 計算を 1 ずらす |
| 50 | `test_benchmark::TestLatencyStatistics::…空で理由` | 空判定を削る |
| 51 | **`test_benchmark::TestColdStart::…親より小さいRSS`** | `VmHWM` の読み取りを `resource.getrusage(RUSAGE_SELF).ru_maxrss` に置き換える（§2.7 の pin） |
| 52 | `test_benchmark::TestColdStart::…子プロセスで測る` | 子プロセスを起こさず親プロセスで測る |
| 53 | `test_benchmark::TestDeviceBenchmark::…worst_p95が最大` | `max` を `min` にする |
| 54 | `test_benchmark::TestDeviceBenchmark::…warmupを回す` | warm-up loop を削る（初回 session 実行が measured に混ざる。判定が不安定なら計測回数で代替する） |
| 55 | `test_benchmark::TestDeviceBenchmark::…artifact_bytes` | payload 合計を model.onnx 単体にする |
| 56 | **`test_model::TestExportedShapes::…batchが特殊化されない`**（commit 2） | `heads.py` の比較に `int()` を戻す |
| 57 | **`test_model::TestExportedShapes::…mask経路で高さ幅が残る`**（commit 2） | `blocks.py` の比較に `int()` を戻す |
| 58 | **`test_architecture::TestInferenceOnlyLayer`** | `ml/export/runtime.py` に `import torch` を足す |
| 59 | `test_architecture::TestDependencyFreeLayer` | `ml/export/manifest.py` に `import onnxruntime` を足す |
| 60 | `test_architecture::TestRuntimeLayer` | `ml/export/parity.py` に `import onnx` を足す |
| 61 | `test_architecture::TestDomainIndependence` | `ml/export/promotion.py` に `from pcbasm.config import Machine` を足す |
| 62 | `test_integration::…INT8のlatencyを落とすと却下` | #7 と同じ（通し経路なので広く落ちる。単独の機構検証には使わない） |

太字の 18 行（#7, #8, #13, #18, #23, #25, #26, #28, #29, #33, #34, #41, #42, #45,
#51, #56, #57, #58）は **この MR の存在理由そのもの**なので、変異実験を必ず通すこと。

---

## 8. 規約チェックリスト（実装者向け）

- `src/ml/` から `pcbasm` / `web` を import しない（相対 import での迂回も不可）
- **ABC を増やさない。** `onnxruntime.quantization.CalibrationDataReader` の実装は
    3rd-party API の要件であって我々の抽象ではない。private class にする
- **Protocol は使わない。** `@override` は ABC 実装にのみ付く
- `@attrs.frozen`。`@dataclass` は使わない。Tensor / ndarray を持つ型だけ `eq=False`
    （`PreprocessedSample` / `GaussianBatch` と同じ理由をコメントに書く）
- 検証は例外ではなく `validate() -> str | None`。失敗は
    `tuple[T | None, str | None]`。**`onnx` / `onnxruntime` / `torch.onnx` が投げる
    例外はすべて捕まえて理由文字列にする**（環境依存の失敗が呼び出し側へ漏れない）
- 唯一の例外は「呼び出し側の不変条件違反」で、`ActivePointer.switch` の
    `ValueError` と同じ扱い。新規に増やさない
- クラスに属する関数は module-level に置かず classmethod / メソッドにする。
    MR6 で増える型: `TensorContract` / `InferenceManifest.load` /
    `PromotionDecision.decide` / `OnnxGraphSummary.inspect` /
    `OnnxExportResult.export` / `StaticQuantizationResult.quantize` /
    `OnnxParityResult.measure` / `OnnxInferenceModel.load` /
    `LatencyStatistics.of` / `BenchmarkCase.write` / `ColdStartMeasurement.measure` /
    `DeviceBenchmark.measure` / `TensorDifference.between`
- 略語を綴りきる: `quantization` / `calibration` / `configuration` /
    `standard_deviation` / `onnxruntime_version` / `peak_resident_kibibytes` /
    `multiply_accumulate`。`qty` / `cfg` / `int8_cfg` を書かない
- docstring とエラーメッセージは日本語
- 内部実装と `__init__` で設定する属性は `_` prefix。`OnnxInferenceModel` の
    session は `_session`
- `__all__` を module 末尾に置く
- `src/ml/export/__init__.py` は **docstring のみ。re-export しない**
- `tests/ml/**` は `--doctest-modules` で collect される。docstring に `>>>` を書かない
- `tests/ml/export/support.py` は `tests.helpers` も `pcbasm` も import しない

### docformatter の落とし穴（MR5 で実測・切り分け済み）

**docformatter はファイルを書き換えるとき、書き換えとは無関係な位置の日本語文字を
化けさせる**（`実` U+5B9F → `殟` U+6B5F、`内` U+5185 → `憅` U+61C5）。
根本原因は未解明。防御は「書き換えさせない」ことのみ。

- docstring の summary は `--wrap-summaries=79` で折り返しが起きない長さに保つ。
    判定は表示幅ではなく `len()`
- 説明部は **1 文 1 段落**。1 文ごとに空行で区切る
- **summary を小文字の識別子・英単語で始めない**（先頭が大文字化される）。
    `onnx graph を検査する` ではなく `ONNX graph を検査する`
- **新規ファイルを含む変更では `git add -A` してから `make format` を走らせる。**
    pre-commit は git が知っているファイルだけを対象にするので、未追跡ファイルは
    `make format` が 2 回連続で pass しても一度も検査されていない
- docstring を足したら `--check` が exit 0（書き換えゼロ）であることを確認する

### 検証

```bash
git add -A && make ml-docker-check   # format -> ML の型検査 -> tests/ml
```

2 回続けて走らせる（1 回目の format が書き換える）。
ベースラインは **592 passed / 1 skipped**。
**`make test` / `make run` / `pytest -m hardware` は絶対に実行しない。**
`pytest` を直接叩くときは対象パスに関わらず `-m "not hardware"` を付ける。

---

## 9. 想定リスク・トレードオフ

1. **`ml/export/` が 8 module になる。** 1 module 案（計画書 §7 の字面）は Pi の
    推論経路に `onnx` を持ち込むので採れない。3 module 案（export / runtime /
    gate）も検討したが、`quantization` が `onnx` を引く一方 `parity` は引かない
    という依存の非対称（§2.1）が module 境界と一致しないため崩れる。
    分割の代償は import 文の増加だけで、公開 API の数は変わらない
2. **`INFERENCE_ONLY_MODULES` を新設して層が 4 つになる。** 層が増えると
    「どこに置くか」の判断が 1 段複雑になる。代案（`runtime.py` を
    `RUNTIME_MODULES` に入れて torch を許す）は、cold latency gate に対する
    最大の要因（`import torch`）を無防備にする。層の追加は
    `test_architecture.py` の末尾追記で済み、MR5 との競合も起きない
3. **operator 許可リストを `ml` が持たない。** §2.3 のとおり実際の operator 集合は
    model 構成で変わるので、`ml` が固定リストを持つと encoder を変えるたびに
    `ml` を直すことになる。代償として、呼び出し側が
    `verify_standard_operators` を**呼び忘れられる**。`export` の中で強制する案は
    許可集合の既定値を `ml` が持つことになるので採らなかった。
    緩和: Phase 5 のドメイン側 release フローで必ず呼ぶ
4. **INT8 と FP32 の数値 parity を測る API を持たない。** 計画書 §6 の INT8 gate は
    metric（`relative_error_score`、coverage）で定義されており、
    生の tensor 差ではない。`OnnxInferenceModel.predict` の出力を
    `GaussianRegressionMetrics` へ流せば足りる。必要になったら
    `OnnxParityResult.between_sessions` を足す
5. **cold start を子プロセスで測るので、計測が `sys.executable` と環境変数に依存する。**
    親と子の環境が違う（venv、`OMP_NUM_THREADS`）と結果がずれる。
    `sys.executable` をそのまま使い、環境変数は継承する。実機で測るのは
    ユーザーなので、`device_label` に条件を書き残してもらう
6. **`ml.export.runtime` が numpy 前提になり torch tensor を受けない。** ドメインの
    前処理は `ml.data.image` が torch tensor を返すので、推論直前に
    `.numpy()` する 1 行がドメイン側に出る。代案（torch を許す）は 2 のとおり却下
7. **`src/ml/model/` を触るので MR4 の範囲へ手が入る。** 変更は 2 行で eager の
    意味は変わらないが、`tests/ml/model/` に export 由来のテストが増える。
    落としても MR6 の出荷 artifact は成立する（§4.10）ので、レビューで過剰と
    判断されたら commit 2 ごと落とせる
8. **計画書 §6 の「ONNX Runtime optimized FP32」段階を削る。** §2.6 の実測に基づく
    判断だが、docs の変更を伴う。§6 の 5 段 pipeline を読んで実装する人が
    いる可能性があるので、commit 12 で必ず同期する
9. **INT8 が FP32 より大きくなりうる**（§2.5）。`artifact_bytes` を tie-break に
    使う設計はこれを織り込んでいるが、「INT8 = 小さい」という直感とずれる。
    `PromotionDecision.summary` に実測値を書き残す
10. **`tests/ml/export` の実行時間。** ONNX export が 1 回 2〜4 秒かかる。
    module 単位で `tmp_path_factory` の session scope fixture へ寄せ、
    export 回数を抑える。全体で 60 秒を超えたら分割を見直す

---

## 10. 確認事項（orchestrator 経由でユーザーへ）

1. **`ml/export/runtime.py` を torch なしにする方針でよいか。**
    暫定案は「よい」。`INFERENCE_ONLY_MODULES` を新設して機械検証する（論点 1）。
    代償はドメイン側の推論直前に `.numpy()` が 1 行出ること。
    「Pi にどのみち torch が入るので分けなくてよい」という判断もありうるが、
    §6 の cold latency gate（process 起動から初回予測まで）に
    `import torch` が直接効くので分ける価値が大きいと考えた。

2. **計画書 §6 の「ONNX Runtime optimized FP32」を artifact 段階から外してよいか。**
    暫定案は「外す」。実測（§2.6）で保存された graph が
    `com.microsoft` / `com.microsoft.nchwc` の独自 operator を含み、ORT 自身が
    「最適化した機体でしか使うな」と警告する。§6 の「独自 operator は許可しない」
    と両立しない。代わりに Pi 上の session 生成時に `ORT_ENABLE_ALL` を指定する。
    docs も commit 12 で同期する。

3. **`src/ml/model/heads.py` / `blocks.py` の 2 行（`int()` 除去）を MR6 に含めてよいか。**
    暫定案は「含める」。含めなくても MR6 の出荷 artifact（batch 1 固定 +
    高さ・幅 dynamic）は成立するが、含めないと将来 batch を dynamic に
    したくなったとき `ml.model` から直す必要がある。commit 2 として独立させるので、
    過剰と判断されたら丸ごと落とせる。

補足（確認不要、報告のみ）: 計画書 §6 の「padding invariance 評価」と
「不確かさ threshold の走査」は `ml.evaluation` の担当で、MR6（`ml.export`）の
スコープ外とした。どちらも現状 `src/ml/` に未実装。

---

## 11. 参照

- `docs/image-based-dispense-calibration-ml-plan.md` §6（686〜800 行）、§7（801 行〜）、
    Phase 4（988 行付近）、完了条件（1001 行〜）
- `memory/agents/orchestrator/ml-core-5-config-tuning.md`（MR5 の裁定と落とし穴。
    `git show feature/2026-09-07/ml-core-5-config-tuning:<path>`）
- `memory/agents/implementation-planner/ml-core-5-config-tuning.md`（計画書の見本）
- `src/ml/artifact/document.py`（`DocumentKind`）、`package.py`（`ImmutablePackage`）
- `src/ml/evaluation/compile_parity.py`（`ParityTolerance` / `TensorDifference`）
- `src/ml/evaluation/regression.py`（`relative_error_score` /
    `one_standard_deviation_coverage` — promotion gate の入力）
- `src/ml/training/task.py`（`TrainingTask.model` の compile 契約。論点 4）
- `src/ml/model/blocks.py` / `heads.py`（§2.2 の `int()` ガード）
- `tests/ml/test_architecture.py`（層の機械検証）
- `tests/ml/helpers.py` / `support.py`（テストヘルパーの置き場）
- skill `refactor-conventions`、`testing-strategy`、`hardware-test`
