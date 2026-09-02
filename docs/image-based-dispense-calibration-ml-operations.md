# 画像ベース吐出量推定 ML 運用ガイド

## 位置付け

この文書は、[画像ベース吐出量推定 ML 実装計画](image-based-dispense-calibration-ml-plan.md)
に基づく初期実装の運用手順をまとめる。学習、評価、export の formal run は
MLflow Tracking Server を必須とし、データと split の fingerprint が一致した場合だけ
後続処理を許可する。

以下のコマンドは repository root で実行する。`/abs/...` は実環境の絶対 path
へ置き換える。dataset 原本は変更せず、manifest、split、checkpoint、report、
export artifact は `data/paste-volume-ml/` または MLflow artifact store に保存する。

## 環境構築

### dependency group

ML 依存は通常の application dependency から分離されている。

| group        | 用途                                                  |
| ------------ | ----------------------------------------------------- |
| `ml-runtime` | PyTorch、torchvision、ONNX Runtime による前処理と推論 |
| `ml-train`   | `ml-runtime` に加え、Hydra と MLflow を使う学習・評価 |
| `ml-hpo`     | `ml-train` に加え、Optuna Sweeper を使う探索          |
| `ml-export`  | `ml-runtime` に加え、MLflow、ONNX export と検査を行う |

開発・学習用環境に全 group を導入する。

```bash
make setup-ml
```

`make setup-ml` は project の virtual environment を作り直す。既存環境を保持したい
場合は、別環境で必要な group だけを同期する。

```bash
uv sync --locked --group ml-runtime --group ml-train
uv sync --locked --group ml-hpo --group ml-export
```

運用 CLI が optional ML dependency の不足を検出した場合も、表示される
`uv sync --locked --group <group>` を使う。lock を更新する `uv sync` や、
`--locked` を外した復旧手順へ置き換えない。以降の Python command はすべて
`uv run --locked --all-groups` で lock 済みの全 ML groupを使う。

repository は `torch` 2.12 minor と `torchvision` 0.27 minor の組み合わせを lock
している。ARM64 では PyTorch の CPU wheel index を明示的に使う。GPU workstation
では OS、driver、CUDA に合う wheel が lock されていることを PyTorch 公式 install
selector と照合する。GPU driver と CUDA toolkit はこの repository から導入しない。

MLflow server 起動後、環境、RGB decode、モデルの forward/backward、Hydra
compose、MLflow の metric/artifact 読み戻しを smoke test する。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume smoke \
    --tracking-uri http://127.0.0.1:5000
```

GPU を必須とする workstation では `--require-cuda` を付ける。Raspberry Pi 5 では
付けず、CPU の forward/backward と ONNX Runtime を確認する。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume smoke \
    --tracking-uri http://127.0.0.1:5000 --require-cuda
```

## MLflow Tracking Server

formal run は HTTP(S) の tracking URI だけを許可する。`sqlite:///...` を client へ
直接渡す運用や、接続失敗時の console logging への fallback は行わない。

単一 workstation の初期運用例を示す。`MLFLOW_ROOT` には書き込み可能な絶対
path を指定する。

```bash
export MLFLOW_ROOT=/abs/paste-volume-mlflow
mkdir -p "$MLFLOW_ROOT/artifacts"
uv run --locked --all-groups python -m mlflow server \
    --backend-store-uri "sqlite:///$MLFLOW_ROOT/mlflow.db" \
    --default-artifact-root "file://$MLFLOW_ROOT/artifacts" \
    --host 127.0.0.1 --port 5000 --workers 1
```

別 terminal で client 用 URI を設定する。Hydra entrypoint はこの環境変数を
暗黙参照しないため、後述の `logger.tracking_uri` override も渡す。運用 CLI
は `MLFLOW_TRACKING_URI` を既定値として読む。

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
```

tracking URI や Optuna storage URI にcredentialを埋め込んだ値を、Hydra config、
command例、run nameへ保存しない。実装は永続化境界で URI のuserinfo、query、fragmentを
除去するが、これはcredential管理の代替ではない。認証が必要な環境では、tracking server側の
認証機構とprocess環境のsecret注入を使い、文書やversion管理対象fileへ値を残さない。

SQLite backend と local artifact directory は同一 host の小規模運用向けである。複数
workstation から同時利用する場合は、PostgreSQL と共有 artifact store へ移行する。

## dataset の検査と統合

### 提供済み dataset の現状

repository にある `data/paste-volume-datasets` は次のコマンドで全件検査
できる。ZIP は自動探索対象に含まれず、完成済み session directory が読まれる。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume dataset validate \
    data/paste-volume-datasets
uv run --locked --all-groups python -m pcbasm.cli.paste_volume dataset summarize \
    data/paste-volume-datasets
```

2026-09-02 時点の提供データは次の構成である。

- 1 session、60 sample
- 1 machine、1 `(paste_id, paste_lot)` group（lot 未設定）、1 nozzle diameter
- `dot` 42 sample、`line` 14 sample、`area` 4 sample

base training は train / validation / test に 1 session ずつ必要なため、最低 3
session が必要である。また、machine、paste lot、nozzle がそれぞれ 1 group
だけなので、現在の提供データだけではこれらの leave-one-group-out 評価を
実行できない。画像単位の split へ fallback せず、session と group を追加収集する。

### 複数 dataset の利用

原本をコピーせず、source ID 付きの flat composite manifest を作る。出力先は
新規 file にし、既存 manifest を更新しない。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume dataset merge \
    --source machine-a=/abs/dataset-a \
    --source machine-b=/abs/dataset-b \
    --output /abs/manifests/base-2026-09.composite.json \
    --name base-2026-09
```

`--source` の ID は manifest 内で一意である必要がある。同一 session は content
fingerprint で重複除去されるが、同じ session ID の異なる内容、同じ画像集合の
異なる教師値、source directory 外への path は error になる。

複数 root または composite manifest も同じ validate / summarize で扱える。formal
run では再現性のため composite manifest の指定を推奨する。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume dataset validate \
    /abs/dataset-a /abs/dataset-b
uv run --locked --all-groups python -m pcbasm.cli.paste_volume dataset summarize \
    /abs/manifests/base-2026-09.composite.json
```

## 学習と評価

### 共通の制約

Hydra は `python -m pcbasm.pasting.paste_volume.train` または `evaluate` の argv 全体を
所有する。`pcbasm.cli.paste_volume` を介して Hydra override を中継しない。path は
絶対 path を使い、`data.manifest` と `data.roots` は同時に指定しない。

formal run は解決済み config、dependency version、Git commit、dirty flag、dirty diffの
SHA-256/byte数、composite manifest、sample index、split を local run directory と MLflow の
両方へ保存する。任意のsecretを含み得るdiff本文とuntracked file本文は保存しない。
`torch.compile` は既定で有効である。非対応環境で自動的に eager へ fallback しないため、
原因を記録したうえで必要な run だけ `trainer.compile_enabled=false` または
`compile_enabled=false` を明示する。

以下の例は、検査済み composite と稼働中の MLflow server を前提とする。

### base training

base training は少なくとも 3 session を session 単位で train / validation / test
へ分ける。run directory は明示し、後の resume、export、split 再利用の基準にする。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.train \
    experiment=base \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    checkpoint.directory=/abs/runs/base-2026-09-seed42 \
    logger.tracking_uri=http://127.0.0.1:5000
```

formalなbase training / fine-tuningが成功すると、MLflow runを`FINISHED`へ確定した後だけ
`weights.pt.mlflow-success.json`が`weights.pt`の隣へcreate-onlyで作られる。このreceiptは
weightsの絶対pathとSHA-256、run ID、`run_kind`、tracking URIのhashを結ぶ。後続のexportは
local/remote receipt、liveな`FINISHED`状態、weights内のsource run/hash、およびMLflow上の
dataset/split/training protocol tagを照合する。weightsとreceiptは移動・改名せず一緒に保持する。

manifest を事前作成しない場合も、複数 root を同じ composite 解決に通せる。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.train \
    experiment=base data.manifest=null \
    'data.roots=[/abs/dataset-a,/abs/dataset-b]' \
    checkpoint.directory=/abs/runs/base-from-roots \
    logger.tracking_uri=http://127.0.0.1:5000
```

### Raspberry Pi 5 fine-tuning

fine-tuning は新規 run として開始し、同じtracking server上でformal attestationを検証できる
`base-train` runの`weights.pt`だけを初期値に使う。`parent_base_run_id` はweights内のsource
run IDと一致させる。未attestのweights、`finetune`由来のweights、別tracking serverのreceiptは
run開始前に拒否する。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.train \
    experiment=fine_tune \
    data.manifest=/abs/manifests/machine-a-finetune.composite.json \
    checkpoint.initial_weights=/abs/runs/base-2026-09-seed42/weights.pt \
    checkpoint.directory=/abs/runs/machine-a-finetune-v1 \
    parent_base_run_id=BASE_MLFLOW_RUN_ID \
    logger.tracking_uri=http://127.0.0.1:5000
```

Pi profile は CPU、`torch.compile` ON、最大 2,000 optimizer step、学習用 2,048
sample、55 分 deadline、pixel budget 524,288、最大 batch size 4 を使う。stem と
residual stage 1・2 は freeze され、stage 3、MLP/head、learnable padding pixel だけを
更新する。自動生成される fine-tune split には最低 2 session が必要で、test
assignment は持たない。この weights を promotion するには、学習データと重複しない
別の永続済み release test dataset/split が必要である。

### 中断後の resume

resume は fine-tuning と異なり、同じ run の続行である。元 run と同じ dataset、
split、model、optimizer、trainer override と run directory を再現し、
`checkpoint.resume_checkpoint` だけを追加する。MLflow run ID は checkpoint から
復元される。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.train \
    experiment=base \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    checkpoint.directory=/abs/runs/base-2026-09-seed42 \
    checkpoint.resume_checkpoint=/abs/runs/base-2026-09-seed42/latest.ckpt \
    logger.tracking_uri=http://127.0.0.1:5000
```

dataset fingerprint、split fingerprint、config fingerprint、model 構成、optimizer 構成、
run ID のいずれかが異なる場合は resume を拒否する。既に指定した
`data.split_manifest` がある run では、resume 時も同じ指定を残す。

### checkpoint の意味

| file             | 用途                                                                |
| ---------------- | ------------------------------------------------------------------- |
| `latest.ckpt`    | 500 step または 5 分ごと、epoch 終了時に保存する resume 点          |
| `best.ckpt`      | calibration 前の validation NLL が最良の resume 可能 checkpoint     |
| `final.ckpt`     | early stop、deadline、上限到達を含む正常終了時の最終状態            |
| `emergency.ckpt` | 非有限 loss または gradient 検出時の調査・resume 用                 |
| `weights.pt`     | `best.ckpt` の weight と調整済み不確かさを持つ export/evaluate 入力 |

`latest.ckpt`、`best.ckpt`、`final.ckpt`、`emergency.ckpt` は model だけでなく、
optimizer、scheduler、GradScaler、sampler 位置、batch plan、early stopping 状態、
Python/NumPy/PyTorch の RNG 状態を保持する。export に checkpoint を直接渡さず、
`best.ckpt` から自動生成され、formal receiptを伴う`weights.pt`を使う。学習途中に存在する
`weights.pt`やreceiptのないcopyをexport入力として扱わない。

### 評価と frozen test guard

通常の評価対象はvalidationである。学習runが保存した`split.json`を必ず指定し、formal
`weights.pt`、dataset、splitのfingerprintが一致しなければ評価しない。同じlineageに対して
`allow_external_split=true`を余分に指定することも拒否する。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.evaluate \
    weights=/abs/runs/base-2026-09-seed42/weights.pt \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    data.split_manifest=/abs/runs/base-2026-09-seed42/split.json \
    output_directory=/abs/evaluations/base-validation \
    split=validation \
    logger.tracking_uri=http://127.0.0.1:5000
```

formal evaluationは、解決済みHydra configとoverride、dependency version、Git
commit/dirty/diff identity、composite manifest、sample index、split、dataset検査、model summary、
compile preflight、diagnostic reportをlocal outputとMLflowへ保存する。成功時は
`evaluation-validation.json.mlflow-success.json`をMLflow runの`FINISHED`確定後だけ作る。
失敗したrunは`FAILED`となり、success receiptを残さない。

fine-tuned weightsなどを学習lineageと異なる独立dataset/splitでvalidation評価する場合は、
`allow_external_split=true`を明示する。reportとMLflowには
training と evaluation の dataset/split fingerprint が別々記録される。この option は
fingerprint 検査の無効化ではなく、別の永続 split を意図的に評価するための
区別である。

test は候補選択や threshold 調整に使わない。generic Hydra evaluateで明示的な診断を行う
場合だけ、`split=test` と `allow_frozen_test=true` を同時に指定する。
`split=test` だけでは失敗する。ただし、この経路には候補固定とone-shot消費の契約がないため、
結果をrelease evidenceとして扱わない。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.evaluate \
    weights=/abs/runs/base-2026-09-seed42/weights.pt \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    data.split_manifest=/abs/runs/base-2026-09-seed42/split.json \
    output_directory=/abs/evaluations/base-frozen-test \
    split=test allow_frozen_test=true \
    logger.tracking_uri=http://127.0.0.1:5000
```

release 候補の最終 test は、後述の`candidate frozen-test`だけを使う。これは
validation、compile parity、Pi benchmarkで候補を固定したreportを入力に要求し、成功・失敗に
かかわらず同じselectionのfrozen testを一度だけ消費する。

### cross-group validation

machine、paste lot、nozzleごとのleave-one-group-out評価は、専用Hydra entrypointを
multirunで実行する。各dimensionは学習用splitを自動生成するため、`data.split_manifest`は
指定しない。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.cross_validate -m \
    cross_validation.dimension=machine,paste_lot,nozzle \
    'cross_validation.output_directory=/abs/cross/${cross_validation.dimension}' \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    logger.tracking_uri=http://127.0.0.1:5000
```

利用可能なdimensionではgroupごとにfold MLflow runを作り、別のsummary runへ全foldの
診断report、dataset fingerprint、training protocol fingerprintを記録する。各dimensionの
`cross-validation-report.json`には、summary runが`FINISHED`になった後だけ
`cross-validation-report.json.mlflow-success.json`が隣接して作られる。promotionはreport単体を
信用せず、local/remote attestationとlive runを検証してtyped evidenceへ変換する。groupが2個未満、
またはfold内で学習・評価assignmentを作れない場合も画像単位へfallbackせず、
`available=false`のsummary reportを残す。この状態はrelease-blockingであり、machine、paste lot、
nozzleの3 dimensionすべてについて、異なるsummary run IDを持つ`available=true`のreportとreceiptが
揃うまでpromotion evidenceは完成しない。

### Hydra + Optuna HPO

HPO は Hydra multirun と persistent Optuna storage を使う。objective は最小 validation
NLL だけで、test を実行しない。dataset version、split manifest、search config、
storage URI を固定する。SQLite を使う場合は絶対 path が必要である。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.train -m \
    experiment=base hparams_search=base_optuna \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    data.split_manifest=/abs/splits/base-2026-09.json \
    'hydra.sweeper.storage=sqlite:////abs/optuna/paste-volume.db' \
    logger.tracking_uri=http://127.0.0.1:5000
```

既定は TPE、30 trial、1 GPU に対する `n_jobs=1`、1 trial 最大 60 epoch である。
study name は model family、dataset fingerprint、search config fingerprint から自動生成
される。同じ storage と設定で再実行すると完了済み trial を再利用し、
`n_trials` の合計件数まで続行する。結果は sweep directory の
`optimization_results.yaml` と各 trial の MLflow run に残る。

運用前に 1 trial で integration を確認する場合は次の override を追加する。
これも凍結 test は実行しない。

```text
hydra.sweeper.n_trials=1 trainer.max_epochs=1 trainer.compile_enabled=false
```

HPO trialには`hpo.*` tagが付くため、その`weights.pt`はformal receiptが存在してもexport source
として拒否される。`optimization_results.yaml`から最良trialのparameterだけを選び、同じ永続
dataset/splitに対して通常の200 epoch上限で3 seedを個別のbase trainingとして再学習する。

```bash
uv run --locked --all-groups python -m pcbasm.pasting.paste_volume.train \
    experiment=base \
    data.manifest=/abs/manifests/base-2026-09.composite.json \
    data.split_manifest=/abs/splits/base-2026-09.json \
    trainer.max_epochs=200 trainer.seed=SEED \
    checkpoint.directory=/abs/runs/base-best-seed-SEED \
    logger.run_name=base-best-seed-SEED \
    logger.tracking_uri=http://127.0.0.1:5000 \
    trainer.learning_rate=BEST_LEARNING_RATE \
    trainer.weight_decay=BEST_WEIGHT_DECAY \
    trainer.gradient_accumulation_steps=BEST_GRADIENT_ACCUMULATION \
    model.group_norm_groups=BEST_GROUP_NORM_GROUPS
```

`SEED`と`BEST_*`を実値へ置き換え、3個の固定seedで3 runを完了する。MLflowで
`validation_calibrated/gaussian_nll`、`validation_calibrated/mae_ul`、
`validation_calibrated/normalized_error_score`、`validation_calibrated/one_std_coverage`の平均と
標準偏差を比較し、採用seed/configと判断根拠をrelease記録へ残す。この3-seed確認は明示的な
運用判断であり、`promote`が自動集計するpackage gateではない。

## ONNX export から promotion まで

### 基本原則

base model の release pipeline 全体では、同じ dataset と学習 run の `split.json` を
使う。CLI は ONNX に埋め込まれた source run、source checkpoint role/hash、
training dataset/split fingerprint、preprocess schema を照合する。不一致時に split を
作り直したり、手入力値で回避したりしない。

fine-tuned model の training lineage も model artifact 内で不変に保つ。fine-tune の
validation と Pi benchmark は学習 run の validation assignment を使い、frozen test
だけを重複のない独立 release dataset/split で実行できる。report と promoted
manifest は training lineage と release-evaluation lineage を別々保持する。

`export`、`export-parity`、`compile-parity`、`optimize`、candidateの評価・結合・選択・
frozen test・finalize・package、`benchmark`、`promote`はformal MLflow runである。以下では
`MLFLOW_TRACKING_URI`が設定済みであることを前提とする。出力directory/reportは新規pathを使い、
既存artifactを上書きしない。

各formal CLIは、MLflow runが`FINISHED`になり出力fingerprintが確定した後だけ、出力の隣へ
`<output>.mlflow-success.json`をcreate-onlyで作る。このattestationは出力の絶対path、fingerprint、
run ID、tracking serverを結合し、後続処理はlocal fileとMLflow上の写しの一致も検証する。
したがって、出力とattestationは常に一緒に保持し、片方だけを移動・改名・複製しない。
別pathへ配置する必要がある場合は、formal operationをその絶対出力pathで再実行する。

### 1. best weights を ONNX FP32 へ export

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume export \
    /abs/runs/base-2026-09-seed42/weights.pt \
    --output /abs/releases/base-v1/export
```

出力は `model.fp32.onnx` と `export-report.json` である。export は学習中の
compiled wrapperやarbitrary checkpointを受け付けず、formal receiptをlive検証できる
`weights.pt`だけを入力にする。ONNXのbatch dimensionは1へ固定し、height/widthだけを
dynamicにする。出力directoryにも`export.mlflow-success.json`が隣接する。

### 2. validation全件と4 dynamic shapeでexport parityを記録

同じformal `weights.pt`、FP32 ONNX、dataset、永続splitを指定する。persisted validation
assignmentの全sampleに加え、minimum 32 × 32、maximum-area 512 × 512、portrait
1024 × 256、landscape 256 × 1024の4 shapeでeager/ONNX FP32出力を比較する。一部sampleの
抽出確認やexport時のdummy shape確認で代用しない。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume export-parity \
    /abs/runs/base-2026-09-seed42/weights.pt \
    --fp32-model /abs/releases/base-v1/export/model.fp32.onnx \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --output /abs/releases/base-v1/export-parity.json
```

全sampleと4 shapeが許容誤差を満たした場合だけ成功し、
`export-parity.json.mlflow-success.json`を作る。reportはweights/model hash、training
protocol、dataset/split、validationとtrainのsample assignmentへ結合される。

### 3. strict weightsでcompile parityを記録

releaseに使う同じ`weights.pt`、dataset、永続splitから、最小・最大面積・縦長・横長と実際の
training batchについてeagerと`torch.compile`のforward、loss、全trainable gradientを比較する。
release契約のbackend/modeは`inductor` / `default`固定で、graph breakは0件を要求する。失敗時は
reportを保存してMLflow runを`FAILED`にし、success attestationを作らない。別backendでの診断を
このrelease evidenceへ差し替えない。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume compile-parity \
    /abs/runs/base-2026-09-seed42/weights.pt \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --output /abs/releases/base-v1/compile-parity.json
```

reportのsource weights hash、training protocol fingerprint、dataset/split fingerprintは
ONNX候補のlineageと完全一致しなければならない。同じcompile parity evidenceを全候補の
`candidate bind`へ渡す。

### 4. optimized FP32 と static INT8 候補を作成

INT8 calibration には同じ永続 split の train assignment だけを使う。データは
session の寄与が偏らないように選択され、既定で最大 256 sample を使う。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume optimize \
    /abs/releases/base-v1/export/model.fp32.onnx \
    --calibration-data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --output /abs/releases/base-v1/optimized
```

出力候補は `model.optimized.fp32.onnx` と `model.int8.qdq.onnx` である。INT8 は生成
されただけでは採用しない。

### 5. validation で候補を評価

各候補を元の FP32 export と比較し、validation だけで精度、coverage、padding、
parity、不確かさ threshold の gate を計算する。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate evaluate \
    /abs/releases/base-v1/optimized/model.optimized.fp32.onnx \
    --fp32-reference /abs/releases/base-v1/export/model.fp32.onnx \
    --model-format onnx-fp32 \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --output /abs/releases/base-v1/fp32-validation.json

uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate evaluate \
    /abs/releases/base-v1/optimized/model.int8.qdq.onnx \
    --fp32-reference /abs/releases/base-v1/export/model.fp32.onnx \
    --model-format onnx-int8-qdq \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --output /abs/releases/base-v1/int8-validation.json
```

`--data` には 1 個の composite manifest、または複数 dataset path を続けて渡せる。

### 6. Raspberry Pi 5 で benchmark

validation の小・中・大・縦長・横長の代表 sample を、production と同じ
`preprocess_rgb_pair` と CPU ONNX Runtime で実測する。promotion に使える report は
Raspberry Pi 5上で作成され、5 categoryには異なる5 sampleが必要である。小・中・大は異なる
画像面積、縦長・横長はそれぞれのaspect ratioを満たさなければならず、不足時はreportを作らない。
候補ごとに実行する。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume benchmark \
    /abs/releases/base-v1/optimized/model.optimized.fp32.onnx \
    --model-format onnx-fp32 \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --power-condition 'official 27W PSU' \
    --cooling-condition 'active cooler; ambient 25C' \
    --output /abs/releases/base-v1/fp32-benchmark.json

uv run --locked --all-groups python -m pcbasm.cli.paste_volume benchmark \
    /abs/releases/base-v1/optimized/model.int8.qdq.onnx \
    --model-format onnx-int8-qdq \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --power-condition 'official 27W PSU' \
    --cooling-condition 'active cooler; ambient 25C' \
    --output /abs/releases/base-v1/int8-benchmark.json
```

各category/sampleを10回warm-up後に100回計測し、category別のp50/p95/p99を記録する。
全categoryのp95がそれぞれ1秒以内であることがgateであり、全sampleをまとめたaggregate値だけでは
代用できない。process起点のclockによるcold latency、OS ID/release、Python/ONNX Runtime、
platform、CPU governor、operatorが記録した電源・冷却条件もreportへ残す。productionと同じ
OS imageとruntimeを使い、他の高負荷processを止めて実行する。

### 7. export/compile parity、validation、benchmarkを結合し、候補を固定

`bind`はformal export parity、compile parity、validation、benchmarkのreportと実際のmodel
hash/lineageを結合する。
`select`はすべてのgateを通過した候補のうちPi p95が最小のものを固定する。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate bind \
    /abs/releases/base-v1/optimized/model.optimized.fp32.onnx \
    --evaluation /abs/releases/base-v1/fp32-validation.json \
    --benchmark /abs/releases/base-v1/fp32-benchmark.json \
    --compile-parity /abs/releases/base-v1/compile-parity.json \
    --export-parity /abs/releases/base-v1/export-parity.json \
    --output /abs/releases/base-v1/fp32-candidate.json

uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate bind \
    /abs/releases/base-v1/optimized/model.int8.qdq.onnx \
    --evaluation /abs/releases/base-v1/int8-validation.json \
    --benchmark /abs/releases/base-v1/int8-benchmark.json \
    --compile-parity /abs/releases/base-v1/compile-parity.json \
    --export-parity /abs/releases/base-v1/export-parity.json \
    --output /abs/releases/base-v1/int8-candidate.json

uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate select \
    /abs/releases/base-v1/fp32-candidate.json \
    /abs/releases/base-v1/int8-candidate.json \
    --output /abs/releases/base-v1/selection.json
```

`dependency-complexity-rank` は p95 差が 5% 未満の場合の tie-break に使う。小さい値が
より単純な候補を表す。省略時は FP32 が 0、INT8 が 1 になる。実運用の
dependency 構成がこれと異なる場合だけ明示し、根拠を release 記録に残す。

### 8. 選択済み候補を frozen test で 1 回だけ評価

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate frozen-test \
    /abs/releases/base-v1/selection.json \
    --fp32-reference /abs/releases/base-v1/export/model.fp32.onnx \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --output /abs/releases/base-v1/frozen-test.json

uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate finalize \
    /abs/releases/base-v1/selection.json \
    --frozen-test /abs/releases/base-v1/frozen-test.json \
    --output /abs/releases/base-v1/finalized.json
```

test gate 失敗後に同じ test へ合わせて threshold やモデルを調整しない。原因を
修正する場合は新しい dataset version または外部 holdout を用意する。
実行を開始した時点でselectionの隣へ`<selection>.frozen-test-consumed.json`がcreate-onlyで
作られる。失敗時も削除せず、output pathを変えて同じselectionを再評価しない。

fine-tuned model は学習 split に test がないため、選択後に独立 holdout を明示
する。それ以前の validation、benchmark、bind/select には fine-tune run の dataset
と `split.json` を使う。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate frozen-test \
    /abs/releases/machine-a-v1/selection.json \
    --fp32-reference /abs/releases/machine-a-v1/export/model.fp32.onnx \
    --data /abs/manifests/machine-a-release-holdout.composite.json \
    --split-manifest /abs/splits/machine-a-release-holdout.json \
    --allow-external-split \
    --output /abs/releases/machine-a-v1/frozen-test.json
```

external split の test sample ID は、学習時の train sample ID と candidate validation
sample ID のどちらとも重複できない。`finalize` はこの sample 集合と training /
evaluation fingerprint を再検証する。`--allow-external-split` なしでは training lineage と
異なる dataset/split を拒否する。

### 9. promoted model package を作成

`promote` は validation、Pi benchmark、frozen test の数値 gate を再計算し、train
assignment から coverage を導出する。coverage を手入力して回避できない。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume promote \
    /abs/releases/base-v1/finalized.json \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --cross-validation /abs/cross/machine/cross-validation-report.json \
    --cross-validation /abs/cross/paste_lot/cross-validation-report.json \
    --cross-validation /abs/cross/nozzle/cross-validation-report.json \
    --model-name paste-volume-resnet-small-v1 \
    --model-version base-2026-09 \
    --output /abs/models/paste-volume-resnet-small-v1-base-2026-09
```

promotionはmachine、paste_lot、nozzleを各1件、計3件のformal cross-validation evidenceとして
要求する。3 reportは同じdataset sample集合とtraining protocolへ一致し、異なるsummary run ID、
同じtracking server、`available=true`でなければならない。

出力packageはschema v2の`manifest.json`、`model.onnx`、`preprocess.json`、
`evaluation.json`、`SHA256SUMS`の5 fileだけを持ち、追加fileやsymlinkを許可しない。
`SHA256SUMS`は自身を除く4 fileを列挙する。production loaderはnetworkやMLflowを必要とせず、
全checksum、schema、runtime version、input/output契約、training weights attestation、
export/compile parity、3 cross-validation evidence、評価根拠、training coverageをpackage内の
固定情報だけから再検証する。formal CLIで行うremote attestation検証はpackage作成前の境界であり、
runtime時のoffline検証とは分ける。
fine-tuned model の `promote --data` と `--split-manifest` には、external holdout ではなく
fine-tune training の dataset と split を渡す。manifest の `release_evaluation` には
validation と external frozen test の fingerprint が別々記録される。

validation 後に production と分離した診断・配布用 candidate package が必要な場合
だけ、`candidate package` を使う。これは release pipeline の必須段階ではなく、
production loader からは拒否される。入力にはmodelとevaluationを個別指定せず、compile parity、
validation、benchmarkを結合済みのcandidate reportを渡す。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume candidate package \
    /abs/releases/base-v1/fp32-candidate.json \
    --data /abs/manifests/base-2026-09.composite.json \
    --split-manifest /abs/runs/base-2026-09-seed42/split.json \
    --model-name paste-volume-resnet-small-v1 \
    --model-version base-2026-09-fp32-candidate \
    --output /abs/candidates/base-2026-09-fp32
```

## active model、推論、機体設定

### activate と rollback

promotion 後の package はそのまま不変の directory として保持し、active pointer
を atomic に切り替える。`activate` は package を再検証し、既存 active model を
`previous_model_path` として残す。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume activate \
    /abs/models/paste-volume-resnet-small-v1-base-2026-09 \
    --pointer /abs/models/paste-volume-active.json
```

問題がある場合は、直前 package を再検証して active/previous を入れ替える。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume rollback \
    /abs/models/paste-volume-active.json
```

rollback は pointer に previous model がない場合、または previous package の検証に失敗する
場合は実行しない。自動 rollback は行わない。

### 単発推論

promotion 済み package と lossless RGB PNG の pre/post pair を使う。

```bash
uv run --locked --all-groups python -m pcbasm.cli.paste_volume infer \
    /abs/models/paste-volume-resnet-small-v1-base-2026-09 \
    /abs/sample/pre.png /abs/sample/post.png \
    --pixel-per-mm 30.0
```

出力には model identity と `mean_volume_ul`、`std_volume_ul`、`relative_std`、
`accepted`、`rejection_reason` が含まれる。低信頼度や coverage 範囲外は予測結果の
`accepted=false` として扱い、壊れた package や runtime 初期化失敗は load error として
区別する。

### `machine.toml`

機体設定の `model_package` には promotion 済み package directory または active
pointer file を指定できる。相対 path は `machine.toml` がある directory から解決
される。rollback を運用する場合は pointer file を指定する。

```toml
[paste_volume]
model_package = "/abs/models/paste-volume-active.json"
```

`[paste_volume]` table 自体を省略すると画像ベースの自動補正は無効になる。設定
があるのに model load に失敗した場合は、paste job 開始時に従来の
`rotations_per_ul` で続行するかを prompt する。

model が有効な場合、purge を除く最初の 3 pad の pre 画像を先に撮影し、通常の
route でそれらを塗布した後に post 画像を撮影する。accepted sample が 1 個以上
ある場合だけ job 内の係数を集約し、旧値の 1/3 から 3 倍へ clamp して残り
pad へ適用する。model 予測、crop、集約のいずれかが失敗した sample は棄却し、
有効 sample がない場合は係数を変更しない。

## 現在のrelease evidence

2026-09-02時点ではML pipelineのcodeと検査境界は実装済みだが、releaseのempirical evidenceは
未完成である。実装の存在を実測済みと読み替えない。

| 項目               | code上の状態                                      | 現在の実測状態                     | releaseへの影響                   |
| ------------------ | ------------------------------------------------- | ---------------------------------- | --------------------------------- |
| 提供dataset        | validate/merge/summarize実装済み                  | 1 session、60 sample               | 3-way base split不可              |
| cross-group        | 3 dimensionのfold/summary attestation実装済み     | machine/lot/nozzle各1 group        | 全dimension unavailable、blocking |
| compile/export候補 | formal weights、両parity、schema v2検証を実装済み | formal base weights/split未作成    | blocking                          |
| Pi benchmark       | 5 category・各100計測のgate実装済み               | Raspberry Pi 5で未実施             | blocking                          |
| 塗布フロー         | 推論・棄却・集約・clamp・残りpad適用を実装済み    | camera/stage/dispenser実機で未実施 | blocking                          |

追加sessionとgroupを収集し、base training、cross-group validation、candidate release pipeline、
Pi benchmark、実機塗布確認を完了するまで、promoted modelがrelease可能とは扱わない。

## Raspberry Pi 5 と実機での最終確認

次の確認は実機の camera、stage、dispenser を動かすため、開発環境の自動 test
に混ぜない。実機を確保した運用者が、promotion する package と production 設定で
実施する。

1. Pi で `dataset validate` と `infer` を実行し、RGB channel、画像 shape、
    `pixel_per_mm` が実運転と一致することを確認する。
2. production と同じ OS image、ONNX Runtime、CPU governor、電源、冷却で
    `benchmark` を行い、cold latency、p50/p95/p99、peak RSS、artifact size を
    MLflow に残す。小・中・大・縦長・横長の各sampleでp95が1秒以内であることを
    確認する。
3. active pointer を切り替え、API process を起動し直した後に package checksum、
    model ID、runtime version の load smoke が通ることを確認する。
4. purge 対象が calibration 3 pad に数えられず、対象 3 pad の pre 撮影が塗布前に
    完了し、各 post 撮影と予測が同じ pad/crop に対応することを確認する。
5. accepted sample がある場合、係数が全 calibration pad の集約後に 1 回だけ
    更新され、残り pad だけに適用されることを確認する。clamp の有無、旧値、
    新値、採用/棄却数、棄却理由、model lineage が job result に残ることも確認する。
6. model 不在、checksum 不一致、全 sample 低信頼度、1 sample の推論失敗、非有限
    集約の各経路を確認する。有効 sample が残ればそれだけを集約し、0 件なら
    `rotations_per_ul` を変更しない。
7. 切り替え前 package へ `rollback` で戻し、再起動後の model ID が戻ったことを
    確認する。

実機がない開発 host では hardware test を実行せず、次で non-hardware suite を
検証する。

```bash
make test-no-hardware
```
