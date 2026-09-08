# MR6（`ml.export`）裁定記録

コア ML 基盤 6 本の最後の 1 本。計画書 §6 / Phase 4 に対応する。
作業ブランチは `feature/2026-09-08/ml-core-6-export`（`main` = 56f6fab から分岐）。

MR5（!205）は未 merge だが、`ml.export` は `ml.config` / `ml.tuning` に依存しないので
`main` から分岐した。stacked にすると引き継ぎ資料が警告する pipeline の問題を踏む。

**merge 時に `tests/ml/test_architecture.py` が MR5 と競合する見込み。** 双方が層登録を
足すため。追加位置を整列上離れる場所と末尾へ寄せて競合を局所化する方針にした。

## 決定 1: Pi の推論 runtime は torch なし

`ml/export/runtime.py` は onnxruntime + numpy だけで動かす。新設層
`INFERENCE_ONLY_MODULES` が「torch も禁止」を subprocess で機械検証する。

根拠は planner の実測（orchestrator も独立に確認した）。

```
import onnxruntime              -> onnx  未ロード
import onnxruntime.quantization -> onnx  ロードされる
onnxruntime は torch を読まない
```

この非対称が module 境界を決めた。計画書 §7 は `ml.export` を「ONNX、quantization、
parity、benchmark、runtime」と 1 つに書いていたが、そのままだと Pi の推論経路が
`onnx` / `onnxscript` を引き込んで壊れる。

torch を除く直接の理由は §6 の cold latency gate。`import torch` がそこに直接効く。
代償はドメイン側に `.numpy()` が 1 行出ることで、ユーザーが受け入れた。

| module | 層 |
| --- | --- |
| `manifest.py` / `promotion.py` | `DEPENDENCY_FREE_MODULES` |
| `runtime.py` / `benchmark.py` | `INFERENCE_ONLY_MODULES`（新設） |
| `parity.py` | `RUNTIME_MODULES` |
| `graph.py` / `onnx_export.py` / `quantization.py` | 層登録なし |

## 決定 2: ORT optimized FP32 を artifact から外す

計画書 §6 は「ONNX Runtime graph optimization を有効にした FP32」を候補に挙げていたが、
**保存すると `com.microsoft.nchwc` の独自 operator が入る**（ORT 自身が「最適化した機体で
しか使うな」と警告する）。§6 の「標準 operator だけで graph を構成する」と両立しない。

保存するのは素の ONNX FP32 と static INT8 の 2 種にする。graph optimization 自体は
実行時の session 作成で従来どおりかかるので、速度の利得は失われない。

docs もこの実態へ同期する。

## 決定 3: `ml.model` の `int()` 除去を MR6 に含める

`int(tensor.shape[i])` が dynamic 次元を黙って固定するため、外さないと §6 の
「batch、高さ、幅を dynamic dimension にする」が成立しない。MR6 の目的に直接必要な変更で、
ついでのリファクタではない。eager の振る舞いは変わらない。

外すのは動的次元を固定する 2 箇所だけ（orchestrator が該当行を確認済み）。

- `blocks.py:310` — mask 検査が batch・高さ・幅を固定する
- `heads.py:141` — conditioning の batch 照合が batch を固定する

チャンネル数の検査（`blocks.py:299`、`heads.py:119`、`heads.py:135`）は静的なので外さない。

## planner の実測で設計が変わった事実

- **`output_names=["mean", ...]` は `onnx.checker` が SSA 違反で落ちる**（graph 内部に
    `mean` が既にある）
- **static INT8 は既定設定で失敗する** — `Quantization parameter shared mode is not
    supported for weight yet`。dynamo exporter が GroupNorm の ones/zeros initializer を
    重複排除するため。`op_types_to_quantize=["Conv","Gemm"]` で通る
- **`torch.compile` した module を `torch.onnx.export` へ渡しても例外にならず黙って通る。**
    train mode でも警告だけ。どちらも silent failure なので、`state_dict()` の
    `_orig_mod.` 前置きで拒否し `eval()` + `no_grad()` を強制する
- **`ru_maxrss` は子プロセスが親の high-water mark を継承する。** peak RSS は
    `/proc/self/status` の `VmHWM` から取る

## その他の判断

- **`tests/ml` に hardware テストを 1 件も置かない。** `tests/ml` は `tests/helpers`
    （pcbnew / picamera2 依存）を参照できず、`make ml-docker-*` は全部 `-m "not hardware"`
    なので死んだテストになる。benchmark は `src/ml/export/benchmark.py` に機構だけ置き、
    実行は Phase 5 の運用ジョブ。実機テストが要るなら `tests/pcbasm/` 側
- **INT8 の採否は `PromotionDecision.decide()` という事後の純関数**で表現する。
    MR5 の `StudyResults.verify_lineage()` と同型。`LatencyEvidence`（実機実測）が `None` の
    候補は構造的に promote 不能なので、「生成しただけで採用しない」がコードの形になる
- §6 の「padding invariance 評価」と「不確かさ threshold 走査」は `ml.evaluation` 担当で
    MR6 のスコープ外（どちらも現状未実装）

## MR5 から引き継ぐ工程

- シグネチャをレベル確定させてから `spec-test-author` と `plan-implementer` を並列起動
- 合流後に変異実験フェーズを別に置く（MR5 では 95 変異で初回 survivor 6 件を検出）
- **値の範囲だけを見るテストは確率的に嘘をつく。** MR5 で `step` を潰す変異が
    33% の確率ですり抜けた。観測点は決定的なものを選ぶ
- docformatter に書き換えさせない（[decision_rustuna.md](../../decision_rustuna.md) と
    同じく `memory/` 側にも記録済み。詳細は MR5 の裁定記録）

## 実装フェーズ中の追加裁定（3 件）

### 追加裁定 1: `int()` 除去は残す。根拠は 3 通りに分離された

`spec-test-author` が「commit 2（`int()` 除去）は torch 2.12.1 では機能的に no-op」と報告し、
planner の §2.2 と食い違った。orchestrator が `Constraints violated` を実測し、
最終的に実装者が 3 者を分離した。**3 つの主張はすべて、各々の条件で正しかった。**

| 宣言 | `int()` あり | `int()` なし |
| --- | --- | --- |
| `Dim.AUTO` + `strict=False`（torch 2.12 の既定） | placeholder が `(2, 4)` へ特殊化（宣言が黙って無視される） | `(s3, 4)` |
| 名前付き `Dim` + `strict=False` | `UserError: Constraints violated (batch)!` | OK |
| `strict=True`（dynamo） | `(s27, 4)`。特殊化しない | `(s27, 4)` |

`torch.onnx.export(dynamo=True)` は `int()` の有無に関わらず `dim_param='batch'` を出す。
silent fallback があるので最終 ONNX が同じに見えていた。

planner の「黙って固定される」は `Dim.AUTO` で正しい。orchestrator の
`Constraints violated` は名前付き `Dim` で正しい。`spec-test-author` の「ONNX の次元が
同じ」も正しい。**「機能的に no-op」だけが誤り。**

`int()` 除去は将来への保険ではなく、**現に存在する制約違反の除去**であり、
silent fallback への依存をやめる変更。ユーザー決定 3 は変わらない。

`#56` / `#57` は既存の `TestExportedDynamicShapes` が既に検出していた
（`torch.export.export` の既定が `strict=False` のため）。既定変更で検出力が消えないよう
`strict=False` を明示的に書いた。

### 追加裁定 2: manifest は precision と quantization を結び付ける

`InferenceManifest.validate()` で結び付ける。

- `precision="static-int8"` なのに `quantization is None` → 存在し得ない artifact を記述している
- `precision="float32"` なのに `quantization is not None` → 矛盾

manifest は Pi へ出荷される単一の記述なので、内部整合は `validate()` の担当。
`PromotionCandidate` 側は変更しない（§4.2 が独立条件で書かれ、`decide()` が
`calibration_split` を見ている）。

### 追加裁定 3: compile 済み判定を `_orig_mod` 属性で行う

`state_dict()` の前置き走査から `hasattr(model, "_orig_mod")` へ置き換えた。

`spec-test-author` の報告「`torch.compile` した parameter 無し module は `state_dict()` が
空になり `_orig_mod.` 前置き判定をすり抜ける」を orchestrator が実測確認した。

```
NoParams  plain     state_dict=[]  prefix判定=False  _orig_mod属性=False  type=NoParams
NoParams  compiled  state_dict=[]  prefix判定=False  _orig_mod属性=True   type=OptimizedModule
```

機構を増やすのではなく、より確実な 1 つに寄せた。

## 変異実験フェーズの裁定

- **#48（`ORT_DISABLE_ALL`）は survivor から外す。** graph 最適化は出力を変えないという
    契約なので、変異が意味的に no-op。テストの穴ではない
- **#54（warm-up）は `warmup_count` の記録を契約とする。** predict 回数の観測には
    3rd-party 表面のモックが要り、モック禁止と両立しない
- **`FutureWarning` 17 件は抑制しない。** torch 内部の deprecation で `copy.deepcopy` 経路から
    出る。呼び方を変えても消せない。抑制すると torch 上げ時に消えたことが分からなくなる。
    将来ノイズになったら `ignore::FutureWarning` ではなく message match の 1 行にする

## レビュー 1 巡目の裁定（must-fix 1 件 / should-fix 9 件）

**M1: 精度 gate 3 分岐の空洞化。** orchestrator が変異で独立確認した。絶対
`primary_score` gate を `if False:` に潰しても `test_promotion.py` の 23 件すべてが緑。

判定順が `絶対primary_score → 絶対coverage → baseline比regression → baseline比coverage差`
で、先の分岐が後を隠している。baseline 比 coverage 差の分岐にはテストが 1 本も無かった。

`ml.evaluation` の metric 待ちにはせず今回埋める。**分岐は今このコードに存在していて、
テストが守っていないだけ**だから。

**これは #11（`candidate_id` の辞書順が偶然 float32 を選んでいた）と S4
（`validate()` の未テスト分岐）と同型で、この MR で 3 度出たパターン。**
「先の分岐が後を隠す」空洞化が他の `validate()` にも無いかを洗わせた。

should-fix の裁定: S1（`quantized_operator_types=()` を入口で弾く）、S2（公開 API が
`KeyError` を投げるのを理由文字列へ）、S3（到達不能な `raise` を削る）、S4（未テスト分岐を
埋める）、S5（到達不能なら dedup ごと削る。実装者判断）、S8（テストノートの訂正前記述を
直す）はいずれも修正。S9（本記録の追記漏れ）がこの節。
