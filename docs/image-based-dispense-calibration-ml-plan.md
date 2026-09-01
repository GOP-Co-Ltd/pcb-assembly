# 画像ベース吐出量推定 ML 実装計画

## 位置付け

この文書は、[画像ベース吐出量キャリブレーション要件](image-based-dispense-calibration.md)
に定義されたデータセット収集の次段階として、データ読み込みからモデルを実際の塗布フローで
利用するまでの実装方針を固定する。

初期実装は単一 view の塗布前後画像から体積と不確かさを推定する。複数 view モデル、学習用
WebUI、分散学習、モデルregistryによる自動配布は対象外とする。

主要な判断は次のとおりとする。

| 項目            | 採用方針                                                                         |
| --------------- | -------------------------------------------------------------------------------- |
| 学習framework   | PyTorchのみ。PyTorch Lightningは使わない                                         |
| 画像補助library | 既存のNumPy/OpenCVを使い、torchvisionは追加しない                                |
| 実験管理        | MLflow Trackingへ明示的に記録する                                                |
| モデル          | 3段のdownsampling stem、small ResNet encoder、Global Average Pooling、2 head回帰 |
| 学習単位        | 1 session・1 pad・1 viewを一意に識別し、同じpadのviewは同じsplitへ置く           |
| 推論artifact    | ONNXを基準形式とし、ONNX RuntimeのFP32とINT8を実測比較する                       |
| Pi上の推論      | 精度gateを通った候補のうち、Raspberry Pi 5で最速のartifactを採用する             |
| 中断再開        | modelだけでなくoptimizer、sampler位置、乱数状態を含むcheckpointから再開する      |

## 0. 下準備

### 依存関係

ML依存は通常のWebAPI/UI実行環境へ無条件に入れず、`pyproject.toml` のdependency groupを
分ける。

- `ml-train`: `torch` とMLflow client。学習、評価、ファインチューニングに使用する。
- `ml-export`: `onnx`、`onnxscript`、`onnxruntime`。export、量子化、parity評価に使用する。
- 通常のruntime: Raspberry Pi 5でONNX artifactだけを使う構成では`onnxruntime`だけを
    追加する。Pi上でファインチューニングするときだけ`torch`も追加する。
- `pytorch-lightning`、`torchvision`、`clearml`は初期依存へ追加しない。

CUDA wheelとARM64 CPU wheelは配布元が異なり得るため、実装開始時に
[PyTorch公式install selector](https://pytorch.org/get-started/locally/)で、GPU workstationと
Raspberry Pi 5の双方に存在する同一minor versionを確認してからlockする。versionを文書中の
固定値にはせず、実際に検証したversionを`uv.lock`とMLflow runへ残す。

MLflowは学習loopの依存に直接埋め込まない。`ExperimentLogger` protocolとMLflow adapterの
境界を設け、モデル、loss、optimizer、checkpointはMLflowをimportしなくても動くようにする。
ただし正式な`train` / `finetune` CLIではMLflow loggerを必須とし、接続不能なら学習開始前に
失敗させる。consoleだけへ黙ってfallbackしない。

### 開発環境の確認

依存追加後に、次を自動確認するsmoke commandを用意する。

1. Python、PyTorch、CUDA、cuDNN、ONNX Runtimeのversionを表示する。
2. GPU workstationでは`torch.cuda.is_available()`が真で、CUDA tensorの畳み込みと
    backwardが成功することを確認する。
3. Raspberry Pi 5ではCPUで同じforward/backwardを実行する。
4. 64 × 64と1024 × 256のdummy inputでeager modelがforwardできることを確認する。
5. MLflow tracking serverへtest run、metric、artifactを記録して読み戻す。

GPU driverやCUDA toolkit自体のinstallはrepositoryのsetup scriptへ含めない。OSとdriverの
組み合わせを壊しやすいためである。repositoryはPython packageのlockと上記smoke checkまでを
担当する。

### 保存領域

データと実験生成物を次の3種類に分離する。

```text
data/paste-volume-datasets/     # 収集済みの原本。学習処理は変更しない
data/paste-volume-ml/           # local checkpoint、split、exportの作業領域。Git管理外
MLflow artifact store           # runに紐づくconfig、report、best/final artifact
```

MLflowの初期運用は、GPU workstation上のtracking server、SQLite backend、local artifact
directoryで開始する。複数人・複数workstationから同時利用する段階でPostgreSQLと共有artifact
storeへ移す。MLflowはlocal構成とtracking server構成の両方を提供しているが、run IDを共有できる
よう初期実装からserver経由に統一する。

## 1. データパイプライン

### session探索と検証

入力は1個以上のdataset rootまたはsession directoryとする。loaderは
`kind == "pcbasm-paste-volume-dataset"`かつ`schema_version == 1`の完成sessionだけを読む。
`.tmp`、`.incomplete`、ZIP artifactは自動探索へ含めない。

学習前に次を全件検証し、1件でも不正なら部分的に無視せず失敗する。

- `metadata.json`の型、未知key、有限値、正の`camera.pixel_per_mm`と
    `measured_volume_ul`
- session内で一意なpad index、pad内で一意なview number
- `pre`、`post`、`mask`がsession directory内の相対pathであり、path traversalやsymlinkで
    外へ出ないこと
- 塗布前後が同じ幅・高さの3 channel lossless PNGであること
- maskが画像と同じ幅・高さの単channel PNGで、値が0または255だけであること
- `pixel_rect`の寸法が保存画像と一致すること
- purgeと全padの配分体積合計が`total.measured_volume_ul`と数値誤差内で一致すること
- purgeと全padの正方向回転数合計が`total.rotations`と数値誤差内で一致すること
- 同一画像内容または同一session IDを複数rootから重複登録していないこと

OpenCVでPNGをdecodeした直後にBGRからRGBへ明示変換する。保存契約上のRGBと学習tensorの
channel順を曖昧にしない。

### sample index

1個のviewを1個の`PasteVolumeSample`とし、原本を変更せず、次の情報を持つindexを生成する。

- `sample_id`: dataset fingerprint、session ID、pad index、view numberから作る安定ID
- `session_id`、`machine_id`、`paste_id`、`paste_lot`、`nozzle_diameter_mm`
- `board.signature`、pad ID、view number
- pre/post/maskのpath、元画像の幅・高さ
- 収集時の`pixel_per_mm`と教師`measured_volume_ul`
- session内pad数とpad内view数

複数viewはv1では別sampleとして学習できるが、同じpadの全viewを必ず同じsplitへ入れる。
同じ教師値を持つview数が多いpadを過大評価しないよう、loss weightは次とする。

\[
w_{s,p,v}
=
\frac{1}{N_{\mathrm{pads\ in\ session}\ s}}
\frac{1}{N_{\mathrm{views\ of\ pad}\ p}}
\]

各batchではweight合計でlossを正規化する。これにより、pad数の多い基板やview数の多いpadでは
なく、各収集sessionが同程度の寄与を持つ。

### dataset fingerprintとsplit

dataset fingerprintは、対象sessionごとの`metadata.json`内容、相対画像path、各画像の
SHA-256を安定順に連結してSHA-256化する。生成したsample indexとsplit manifestをJSONLで
保存し、MLflowへartifactとして記録する。

無作為な画像単位splitは禁止する。初期実装は次の評価を分ける。

1. **primary split**: sessionを最小groupとしてtrain / validation / testへ分ける。同一sessionと
    同一padは複数splitへ跨がせない。既定比率は70 / 15 / 15とし、seed固定で再生成可能にする。
2. **cross-machine report**: machine単位のleave-one-group-out評価を行う。
3. **cross-lot report**: `(paste_id, paste_lot)`単位のleave-one-group-out評価を行う。
4. **cross-nozzle report**: nozzle径単位のleave-one-group-out評価を行う。

対象groupが2種類未満のcross-group評価は、画像単位splitへfallbackせず「評価不能」と記録する。
test splitはモデル選択、early stopping、不確かさ補正、export方式選択、INT8 calibrationに
使用しない。候補選択をvalidationで完了してから、選ばれたartifactをtestで1回だけ最終評価する。
split manifestが既に与えられた場合は再分割せず、そのfingerprint一致を要求する。base trainingは
train / validation / testへ最低1 sessionずつ存在すること、fine-tuningはtrain / validationへ最低
1 sessionずつ存在することを要求し、不足時は学習開始前に失敗する。

### 画像サイズ

dataset形式自体には画像上限を設けない。v1モデルへ入れる前処理済み画像には、計算量を
制御するため次の既定値を設ける。

| 制約            |     既定値 | 動作                                           |
| --------------- | ---------: | ---------------------------------------------- |
| 最小の高さ・幅  |      32 px | 片方でも未満なら情報不足としてsampleを拒否する |
| 最大の高さ・幅  |    1024 px | 超える場合は等方downscaleする                  |
| 最大画素数      | 262,144 px | 超える場合は等方downscaleする                  |
| CNNのstride単位 |      32 px | batch padding時だけ32の倍数へ揃える            |

元画像はupscaleしない。最大辺と最大画素数の両方を満たす最大scaleで等方downscaleし、変換後の
`pixel_per_mm`へ同じscaleを掛ける。したがって1024 × 256や512 × 512を扱える。極端に細長い
画像を正方形へ歪めない。

これらはノズル径、体積、塗布方式の制限ではなく、v1 encoderの入力品質・計算量の制約である。
値は学習configとmodel manifestへ保存し、変更したモデルは別versionとして扱う。

### tensor化と正規化

1. pre/postを`float32`の`[0, 1]`へ変換する。
2. 同じ幾何変換をpre、post、pad geometry maskへ適用する。
3. pre RGB、post RGBの順で連結し、`[6, H, W]`とする。
4. train splitだけから6 channelそれぞれのmean/stdを算出し、標準化する。
5. `log(pixel_per_mm)`をtrain splitのmean/stdで標準化する。
6. 教師体積をtrain splitの正の中央値`volume_scale_ul`で割る。

画像統計、scale統計、`volume_scale_ul`はcheckpointと推論artifactへ保存する。検証・test・推論で
統計を再計算しない。`pixel_per_mm`の分散がほぼ0なら除算用stdを1へ固定して警告し、scaleの
汎化を評価不能としてreportへ残す。

収集済みのpad geometry maskは入力channelへ加えない。v1のモデル入力は要件どおり6 channelを
維持し、geometry maskはcrop検証と幾何augmentationの整合確認に使う。batch paddingを示す
`valid_pixel_mask`とは別物である。

### Data Augmentation

要件どおり回転と等方scaleだけを行う。輝度、contrast、色、blur、noiseは初期実装で変更しない。

- 回転角は`[0, 360)`から一様に選び、pre/postへ同じbilinear変換、geometry maskへ同じnearest
    変換を適用する。
- 回転によって元canvas外から入る画素はinvalidとし、後述のlearnable padding pixelで置換する。
- scaleは`[0.8, 1.2]`をlog-uniformで選ぶ。適用後も画像上限制約を満たすようclipし、
    `pixel_per_mm`へ実際のscaleを掛ける。
- validation/testではaugmentationを無効化し、downscaleと正規化だけを適用する。
- augmentationの乱数は`global_seed`、epoch、`sample_id`から導出する。worker数や中断再開で
    同じsampleの変換が変わらないようにする。

### batch生成

固定枚数だけでbatchを作ると、最大画像に引きずられてpaddingとGPU memoryが増える。このため
aspect ratioと面積でbucket化し、pixel budget制のbatch samplerを使う。

1. 前処理後の`log2(width / height)`を0.25刻み、`log2(width * height)`を1.0刻みに丸めて
    bucket keyとする。
2. epochごとに各bucket内をseed付きでshuffleする。
3. sampleを追加した場合の
    `batch_size * ceil32(max_height) * ceil32(max_width)`がbudget以内の間だけ追加する。
4. GPU base trainingの既定値は`max_batch_pixels=8,388,608`、`max_batch_size=32`とする。
5. Raspberry Pi 5 fine-tuningの既定値は`max_batch_pixels=524,288`、
    `max_batch_size=4`とする。
6. 最後の小batchも捨てない。BatchNormは後述のfine-tuningでは常にevalに固定する。

collate時はbatch内の最大高さ・幅を32の倍数へ切り上げ、trainingでは画像の配置位置をランダム、
evaluationでは中央にしてpaddingする。collateは画像tensorと`valid_pixel_mask [B, 1, H, W]`を
返す。モデル側でinvalid位置を学習可能な6 channel pixel vectorへ置換するため、padding値自体に
意味を持たせない。

## 2. モデル

### 入出力

学習時のforward入力は次の3 tensorとする。

```text
image_6ch:       float32 [B, 6, H, W]
valid_pixel_mask: bool  [B, 1, H, W]
pixel_per_mm:    float32 [B, 1]
```

外部の推論APIはpre RGB、post RGB、`pixel_per_mm`を受け取り、単一sample用のall-valid maskを
内部で作る。batch paddingという学習上の都合を塗布ドメインAPIへ漏らさない。

出力は次の2値とする。

- `mean_volume_ul`: Softplusにより正とした平均体積
- `log_variance_volume_ul2`: 数値安定範囲へ制限した対数分散

`std_volume_ul = sqrt(exp(log_variance_volume_ul2))`はモデル利用moduleで計算して公開する。

### v1 encoder

初期モデル`paste-volume-resnet-small-v1`を次で固定する。すべての畳み込みはbiasなし、
BatchNorm2d、ReLUの順を基本とする。residual blockは標準的な2個の3 × 3 convolutionとskip
connectionを持ち、shape変更時だけ1 × 1 projectionを使う。

| 段               | 構成                             | 出力channel | 空間stride |
| ---------------- | -------------------------------- | ----------: | ---------: |
| stem 1           | 3 × 3 Conv, stride 2             |          24 |          2 |
| stem 2           | 3 × 3 Conv, stride 2             |          32 |          4 |
| stem 3           | 3 × 3 Conv, stride 2             |          48 |          8 |
| residual stage 1 | BasicBlock × 2                   |          48 |          8 |
| residual stage 2 | BasicBlock × 2、先頭だけstride 2 |          96 |         16 |
| residual stage 3 | BasicBlock × 2、先頭だけstride 2 |         160 |         32 |
| pooling          | Adaptive Global Average Pooling  |         160 |          - |

入力前に`valid_pixel_mask`が偽の位置を`nn.Parameter([1, 6, 1, 1])`のlearnable padding pixelへ
置換する。このparameterは標準化後の0、すなわちtrain画像の平均色から初期化する。Masked
Global Poolingにはせず、通常のGlobal Average Poolingを使う。

pooling後の160次元特徴へ、標準化済み`log(pixel_per_mm)` 1値を連結する。その後
`Linear(161, 128) -> ReLU`を通し、独立したmean headとlog-variance headへ分ける。画像から
見かけの大きさを学びつつ、物理scaleを明示的に利用できる構成になる。

教師体積を`volume_scale_ul`で正規化した空間で次を計算する。

```text
normalized_mean = Softplus(raw_mean)
normalized_logvar = clamp(raw_logvar, -10, 5)
mean_volume_ul = normalized_mean * volume_scale_ul
log_variance_volume_ul2 = normalized_logvar + 2 * log(volume_scale_ul)
```

meanの上限は設けない。log-varianceの範囲は数値安定性だけを目的とし、体積の対応範囲は
dataset coverageと評価結果で表す。

### モデル規模とfine-tuning範囲

v1は約150万parameter以下を目安とし、512 × 512入力で1.5 GMAC以下であることを実装時に
計測する。parameter数やGMACが上限を超えた場合、精度比較なしにchannelやblockを増やさない。

base modelは全層を学習する。Raspberry Pi 5での既定fine-tuningは次だけを更新する。

- residual stage 3
- pooling後のLinearと2個のhead
- learnable padding pixel

stem、residual stage 1・2はfreezeし、全BatchNormをeval modeに固定してrunning statisticsと
affine parameterを更新しない。これで小batchによるBatchNorm崩れを避け、計算時間を抑える。
全層fine-tuningはGPU用の明示optionとし、Piの既定値にはしない。

## 3. 学習パイプライン

### framework方針

PyTorch Lightningは採用しない。この規模では、必要な機能はdevice転送、AMP、gradient
accumulation、validation、early stopping、checkpoint、loggingに限定され、pure PyTorchで
十分に見通せる。MLflowのvanilla PyTorch autologgingへも依存せず、metricとartifactを明示的に
記録する。

学習処理はCLIから呼べる通常のPython APIとして実装し、CLI parser、学習loop、モデル、data、
logging、checkpointを分離する。configはfrozenな型で表し、CLI引数を一度configへ変換した後は
global stateや環境変数を直接参照しない。

### lossとmetric

学習lossは正規化体積に対するGaussian negative log-likelihoodとする。

\[
L_i
=
\frac{1}{2}
\left(
\exp(-\ell_i)(y_i-\mu_i)^2 + \ell_i
\right)
\]

session/view補正済みweightを掛け、batchのweight合計で割る。PyTorchの既成lossにモデル固有の
暗黙動作を持たせず、この短い式を公開loss functionとして実装する。

毎epochで少なくとも次をtrain / validation別に集計する。

- weighted Gaussian NLL
- MAE [µL]、RMSE [µL]
- 正規化誤差`e`のmean、std、`abs(mean(e)) + std(e)`
- median absolute relative error、95 percentile absolute relative error
- `mean ± 1 std`のcoverage
- 予測stdの平均と、無効値・棄却候補数

0に近い教師値でrelative errorが発散しないよう、評価対象の教師体積は正であるというdataset
契約を要求する。epsilonで見かけ上のerrorを小さくする処理は行わない。

学習終了後、validation splitだけを使ってlog-varianceへ加える1個のscalar offsetをfitし、
不確かさをcalibrateする。mean weightは変更しない。offset固定後にtest coverageを1回だけ評価する。

### optimizerと既定config

| 設定                  |                               base training |                    Pi fine-tuning |
| --------------------- | ------------------------------------------: | --------------------------------: |
| optimizer             |                                       AdamW |                             AdamW |
| learning rate         |                                        3e-4 |                              1e-4 |
| weight decay          |                                        1e-4 |                              1e-4 |
| gradient clip norm    |                                         1.0 |                               1.0 |
| LR scheduler          |   ReduceLROnPlateau, factor 0.5, patience 5 |                              同左 |
| early stopping        | validation NLL, patience 15, min delta 1e-4 |                       patience 10 |
| max epochs            |                                         200 |                               200 |
| gradient accumulation |                                           1 |                                 4 |
| precision             |                          CUDA AMP、CPU FP32 |                          CPU FP32 |
| trainable layer       |                                        全層 | stage 3、MLP、head、padding pixel |

model選択はcalibration前のvalidation NLL最小を第一条件とし、同値ならvalidation MAEが小さい方を
採用する。test metricでcheckpointを選ばない。

### 1 epochの処理

1. modelをtrain modeにし、fine-tuning時はfreeze対象と全BatchNormを再度evalへ固定する。
2. deterministic batch planを生成する。
3. forward、weighted NLL、backward、gradient accumulation、clip、optimizer stepを行う。
4. 非有限lossまたはgradientを検出したら、そのstepを無視せず緊急checkpointを保存して失敗する。
5. epoch末にvalidationを`inference_mode`で実行する。
6. scheduler、early stopping、best checkpointを更新する。
7. metricsと診断artifactをMLflowへ記録する。

CUDAでは`torch.amp.autocast`とGradScalerを使う。CPUではAMPを使わない。`torch.compile`は
baselineへ入れず、学習時間の比較実験としてのみ有効化する。compileの有無はMLflow parameterへ
記録する。

### 再現性

- Python、NumPy、PyTorch CPU/CUDAへ同じrun seedから派生したseedを設定する。
- augmentationはsample単位、batch順はepoch単位の派生seedを使う。
- 初期baselineではdeterministic algorithmを有効にする。未対応opがあれば自動解除せず失敗し、
    性能優先runだけ明示optionで解除する。
- git commit、dirty flag、dependency version、dataset fingerprint、split manifest、全configを
    run開始時に保存する。
- dirty worktreeでの学習は許容するが、diffをartifactとして必ず保存する。

### Raspberry Pi 5の時間制限

fine-tuning CLIはwall-clock deadlineを持つ。既定は55分でoptimizer stepを停止し、残り5分で
validation、checkpoint確定、MLflow flushを行い、全体を1時間以内に収める。

- `max_steps=2000`と`max_epochs=200`の早い方でも終了する。
- datasetが大きい場合は、sessionを均等に残すdeterministic subsetをrun開始前に作る。
- deadlineはbatch間でも確認し、epoch途中でも正常終了checkpointを保存する。
- trainとvalidationへ最低1 sessionずつ確保できないdatasetは学習開始前に拒否する。

## 4. ロギング

### MLflowを採用する理由

初期要求はexperiment trackingであり、remote worker orchestrationやdataset registry全体ではない。
MLflowはparameter、時系列metric、artifact、比較UIに範囲を絞って導入でき、local SQLiteから
shared serverへ同じclient APIで移行できる。ClearMLは自動収集、agent、pipelineまで含めて有用
だが、現段階では運用面と暗黙動作が増えるため採用しない。

MLflowのautologは使わない。pure PyTorchへのautolog対象が限定され、checkpoint形式やresume
契約をこちらで管理する必要があるため、次を明示APIで記録する。

### 1 runに記録する情報

**tag**

- `run_kind`: `base-train`、`finetune`、`evaluate`、`export`、`benchmark`
- git branch、commit、dirty flag、machine ID、parent base run ID
- dataset fingerprint、split manifest fingerprint、model schema version

**parameter**

- model channel/block構成、parameter数、GMAC
- 画像制約、normalization統計のfingerprint、augmentation範囲
- optimizer、scheduler、batch pixel budget、seed、precision、time budget
- Python、PyTorch、CUDA/cuDNN、OpenCV、MLflow、ONNX/ORTのversion

**metric**

- epochごとのtrain/validation lossと全評価metric
- learning rate、epoch時間、sample/秒、peak GPU memoryまたはprocess RSS
- testとcross-groupごとの最終metric
- export parity、artifact size、cold/warm latency、p50/p95/p99

**artifact**

- 解決済みconfig、sample index、split manifest、dataset検証report
- git diff、model summary、学習曲線、予測対真値、残差、coverage plot
- best/final checkpoint、推論artifact manifest、export/evaluation/benchmark report
- 失敗時の最後のcheckpointとfailure reason

raw dataset全体はMLflowへ複製しない。診断画像は固定seedで選んだ少数のpre/post/prediction gridに
限定し、元session pathとfingerprintで追跡する。

MLflowへの一時的なmetric送信失敗はlocal queueへ保持して数回retryできるが、run終了時にflush
できなければ成功扱いにしない。checkpointのlocal atomic saveはMLflow uploadより先に行い、
tracking server障害でresume可能性を失わないようにする。

## 5. checkpointと中断再開

### checkpointの種類

```text
run-directory/
├── config.json
├── split.json
├── latest.ckpt       # 一定stepごとの再開点
├── best.ckpt         # validation NLLが最良の再開可能checkpoint
├── final.ckpt        # 正常終了時の再開可能checkpoint
└── weights.pt        # best modelのstate_dictと推論に必要な統計だけ
```

`latest.ckpt`は5分または500 optimizer stepの早い方、および各epoch末に保存する。
`SIGINT` / `SIGTERM`を受けた場合は新しいbatchを開始せず、現在のoptimizer step境界で
`latest.ckpt`を保存して終了する。

すべて一時fileへ書いて`fsync`後に`os.replace`する。途中書き込みのfileを有効checkpointとして
見せない。`best.ckpt`を上書きする前に新fileの読み戻し検証を行う。

### 保存内容

- checkpoint schema version、run ID、作成時刻
- model構成と`state_dict`
- optimizer、scheduler、GradScalerの`state_dict`
- epoch、global optimizer step、次に読むbatch index
- early stoppingのbest valueとpatience counter
- Python、NumPy、PyTorch CPU、全CUDA deviceの乱数状態
- samplerのepoch seedと、そのepochの確定batch plan
- normalization統計、volume scale、不確かさcalibration前後の状態
- dataset fingerprint、split fingerprint、解決済みconfig

[PyTorchのcheckpoint指針](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html)
に沿い、model全体のpickleではなく`state_dict`を保存する。loadは`weights_only=True`を基本とし、
repository側のmodel classとschema versionから復元する。

### resumeとfine-tuneの区別

`--resume latest.ckpt`は同一runの継続である。dataset fingerprint、split、model構成、optimizer
configが完全一致しない場合は拒否する。batch planとbatch indexから次の未処理batchを再開し、
augmentationもsample派生seedで同一にする。

`finetune base/weights.pt target-dataset`は新しいrunであり、model weightとnormalization初期値だけを
読み、optimizer、scheduler、early stopping、samplerは新規作成する。この2操作を同じoptionで
兼用しない。

## 6. export、最適化、評価

### artifactの段階

学習用checkpointを塗布フローから直接読まない。次の一方向pipelineで推論artifactを作る。

```text
best.ckpt
    -> weights.pt
    -> ONNX FP32
    -> ONNX Runtime optimized FP32
    -> ONNX Runtime static INT8 candidate
    -> 精度・parity・Pi benchmark
    -> promoted model package
```

ONNX exportは`torch.onnx.export(..., dynamo=True)`を使い、batch、高さ、幅をdynamic dimensionに
する。export対象はbatch 1用wrapperであり、入力は標準化済み6 channel画像と標準化前の
`pixel_per_mm`、出力は物理単位のmeanとlog-varianceとする。前処理そのものはmanifestに従う
Python runtime moduleへ残す。

GroupNormや独自operatorは使わず、Conv、BatchNorm、ReLU、Add、GlobalAveragePool、Linear、
Softplus相当の標準operatorへ限定する。これによりexportとARM CPU実行の不確実性を抑える。

### 最適化候補

1. ONNX Runtime graph optimizationを有効にしたFP32。
2. train splitからsession均等に選んだcalibration subsetによるstatic INT8 QDQ。
3. 必要な場合だけ固定shape bucket別artifactまたはchannel数削減を追加実験する。

CNNにはdynamic quantizationではなくstatic quantizationを第一候補とする。INT8は必ずしも速く
ならないため、生成しただけで採用しない。FP16はCPU版ONNX Runtimeの第一候補にせず、Pi上で
明確な対応と高速化が確認された場合だけ比較対象へ加える。

### 評価順序

候補間の比較とthreshold決定はvalidationで行う。以下で「評価sample」と書く箇所は候補選択中は
validationを意味し、artifactを1個に固定した後だけ凍結testへ同じ評価を適用する。

**1. eager model評価**

- primary validationでmodel選択条件を確認し、候補固定後にprimary test、cross-machine、
    cross-lot、cross-nozzleの最終metricを算出する。
- 要件の`abs(mean(e)) + std(e) <= 0.10`をprimary gateとする。
- calibration後の`mean ± 1 std` coverageを68.3%と比較し、bin別reliabilityも記録する。
- 体積、画像面積、aspect ratio、`pixel_per_mm`、塗布方式ごとの誤差をreportする。

**2. padding invariance評価**

同じ画像へ0%、10%、25%、50%の追加paddingを異なる辺へ加える。各条件の予測差を測り、paddingに
よる悪化後もprimary accuracy gateを満たすことを要求する。悪化が1 percentage pointを超える
場合はpromotionせず、bucket幅またはpadding augmentationを見直す。

**3. export parity評価**

全評価sampleでPyTorch eagerとONNX FP32を比較する。meanとlog-varianceが有限で、meanが正、
FP32出力差が`max(1e-6 µL, eager meanの0.1%)`以内であることを要求する。dynamic shapeの最小、
最大、縦長、横長も個別に通す。

**4. INT8精度評価**

INT8もprimary accuracy gateを満たし、FP32に対する
`abs(mean(e)) + std(e)`の悪化が0.01以下、68.3% coverageの差が3 percentage point以下である
ことを要求する。quantization calibrationにはtrain sampleだけを使う。

**5. Raspberry Pi 5 benchmark**

- productionと同じOS、Python、電源・冷却条件で実行する。
- batch 1、CPU、同じ前処理moduleを使う。
- 代表的な小・中・大・縦長・横長sampleを含める。
- process起動から初回予測までのcold latency、10回warm-up後100回のp50/p95/p99、peak RSS、
    artifact sizeを記録する。
- 全sampleのp95が1秒以内であることを要求する。

精度gateを通過した候補のうちPi上のp95が最小のものをpromoteする。差が5%未満なら、artifactが
小さく依存が単純な方を選ぶ。INT8が遅い、または精度gateを落とす場合はFP32を正式artifactに
する。候補決定後に凍結testで全gateを再評価し、失敗した場合はpromoteしない。失敗結果を見て
同じtestへ合わせ込まず、原因修正後は新しいdataset versionまたは新しい外部holdoutを用意する。

### 不確かさthreshold

自動補正に使う`std / mean` thresholdを手入力の固定値にはしない。validation上でthreshold候補を
走査し、採用sample集合がprimary accuracy gateを満たす範囲でcoverageを最大にする値を選び、
manifestへ保存する。threshold決定後、test上で次を確認する。

- accepted sample集合がprimary accuracy gateを満たす。
- 連続する3 sampleから少なくとも1個を採用できる割合が90%以上である。

条件を満たすthresholdがなければ、そのmodelは推論自体には使えても自動補正用にはpromoteしない。

### promoted model package

```text
paste-volume-resnet-small-v1/
├── manifest.json
├── model.onnx
├── preprocess.json
├── evaluation.json
└── SHA256SUMS
```

manifestにはartifact schema version、model名、input/output契約、dynamic dimension、normalization
統計、画像制約、volume scale、不確かさoffsetとthreshold、学習dataset/split fingerprint、
MLflow run ID、exporter/opset/ORT version、quantization方式、各fileのSHA-256を含める。

load時に全checksum、schema version、必要runtime versionを検証する。未知schemaや壊れたartifactを
推測で読み込まない。

## 7. 塗布フローで利用するmodule

### 配置と責務

ML実装は`pcbasm.pasting`の下に置き、WebAPIやUIへ計算を持たせない。実装時の責務境界は次の
とおりとする。

```text
pcbasm.pasting.paste_dataset             # 収集schemaと原本の読み書き（既存）
pcbasm.pasting.paste_volume.data         # validate、index、split、preprocess、batch
pcbasm.pasting.paste_volume.model        # torch modelとloss
pcbasm.pasting.paste_volume.training     # train/fine-tune loop、checkpoint
pcbasm.pasting.paste_volume.experiment   # ExperimentLoggerとMLflow adapter
pcbasm.pasting.paste_volume.export       # ONNX、quantization、parity、package
pcbasm.pasting.paste_volume.inference    # manifest検証、runtime、公開prediction API
pcbasm.cli.paste_volume                   # 薄いCLI
```

通常の`import pcbasm.pasting`でtorch、MLflow、ONNX Runtimeをeager importしない。学習CLIまたは
model loaderを呼んだ時点で必要依存を読み、未installなら必要なdependency groupを示す明確な
errorを返す。

### CLI

装置を動かさないML処理は、repository直下の使い捨てscriptではなく同じ公開APIを呼ぶmodule CLIに
統一する。

```text
python -m pcbasm.cli.paste_volume dataset validate <dataset...>
python -m pcbasm.cli.paste_volume dataset summarize <dataset...>
python -m pcbasm.cli.paste_volume train <dataset...> --config <config>
python -m pcbasm.cli.paste_volume finetune <weights> <dataset...> --config <config>
python -m pcbasm.cli.paste_volume evaluate <weights-or-package> <dataset...> --split <split>
python -m pcbasm.cli.paste_volume export <checkpoint> --output <directory>
python -m pcbasm.cli.paste_volume optimize <onnx-model> --calibration-data <dataset...>
python -m pcbasm.cli.paste_volume benchmark <model-package>
python -m pcbasm.cli.paste_volume infer <model-package> <pre-image> <post-image> \
    --pixel-per-mm <value>
```

`train`、`finetune`、`evaluate`はdataset fingerprintとsplit manifestを常に出力する。
`evaluate --split test`は明示指定を要求し、通常の学習完了処理から自動では呼ばない。`optimize`は
候補packageを作るだけでactive modelを切り替えず、promotionは評価reportを検証する別の公開APIで
行う。

### 公開API

推論側はruntime形式を隠す、概ね次の公開契約にする。

```python
@attrs.frozen
class PasteVolumePrediction:
    mean_volume_ul: float
    std_volume_ul: float
    relative_std: float
    accepted: bool
    rejection_reason: str | None

class PasteVolumeEstimator(Protocol):
    def predict(
        self,
        pre_rgb: ImageArray,
        post_rgb: ImageArray,
        *,
        pixel_per_mm: float,
    ) -> PasteVolumePrediction: ...

def load_paste_volume_estimator(model_package: Path) -> PasteVolumeEstimator: ...
```

loaderはmanifestとchecksumを検証してONNX Runtime sessionを1回だけ作る。`predict`はdatasetと同じ
前処理を共有し、入力shape、RGB、有限で正のscale、画像上限、model coverageを検証する。
modelが返した非有限値、非正mean、manifest threshold超過はexceptionでjob全体を落とさず、
`accepted=False`と具体的なreasonへ変換する。壊れたmodel packageやruntime初期化失敗はload時の
errorとし、予測不能とは区別する。

### 運転時キャリブレーション

既存の`paste_solder` jobへ次の順で統合する。

1. job開始時にmodel packageをloadし、versionとchecksumをjob logへ残す。load失敗時は自動補正を
    無効化し、従来の`rotations_per_ul`で続行するかを既存promptで確認する。
2. purge対象を除く最初の既定3 padについて、同じcrop契約でpre画像を撮る。
3. 通常のrouteで塗布し、同じ撮影位置・cropでpost画像を撮る。
4. estimatorを各padへ1回呼び、mean、std、accepted/reasonを記録する。
5. acceptedなpadが1個以上あれば、要件式どおり推定体積合計と指令体積合計からgainを求める。
6. 新しい`rotations_per_ul`を旧値の1/3から3倍へclampし、残りのpadへ適用する。
7. acceptedなpadが0個、推定例外、計算結果が非有限の場合は設定を変更しない。

推定結果のfilter、集約、clamp、結果型は`pcbasm`コアの公開functionに置く。WebAPI jobは撮影・
塗布の順序、prompt、progress、logだけを担当し、frontendはbackend結果をそのまま表示する。

補正値は全対象padの推論と集約が成功してから一度だけ適用する。途中のpad結果で段階的に
`rotations_per_ul`を変更しない。job結果には旧値、新値、clamp有無、使用・棄却sample、model
versionを残す。

### model配布とrollback

- model packageの配置先はmachine設定から明示pathで指定し、repository packageへweightを同梱
    しない。
- 新artifactは一時directoryへ配置・検証後、active modelを指すsymlinkまたは小さな設定fileを
    atomicに切り替える。
- 直前のartifactを保持し、load smoke testまたは最初の運転時検証に失敗したら手動で戻せるように
    する。自動rollbackやremote自動配布は初期範囲外とする。
- base modelとmachine fine-tuned modelのlineageはmanifestのparent run/checkpoint IDで追跡する。

## 実装順序と検証

### Phase 1: datasetと前処理

- `dataset validate`、`dataset summarize`、sample index、fingerprint、group splitを実装する。
- size制約、paired augmentation、bucket batch sampler、learnable padding用maskまでを通す。
- 実session fixtureでRGB順、pair/mask整合、重複、leakage拒否を確認する。

### Phase 2: modelとpure PyTorch training

- small ResNet、Gaussian NLL、metric、base/fine-tune loopを実装する。
- 数sampleを過学習できること、可変shape batch、padding parameterへgradientが流れることを確認する。
- 中断あり/なしで同じseedの最終weightとmetricが一致するcheckpoint resume testを行う。

### Phase 3: MLflowとCLI

- explicit logger、run config、metric、artifact、failure記録を実装する。
- 実local MLflow serverを使うintegration testでrunとartifactを読み戻す。MLflow APIはmockしない。
- 既定CLIを`dataset validate/summarize`、`train`、`finetune`、`evaluate`まで接続する。

### Phase 4: exportとedge評価

- ONNX FP32 export、parity、static INT8、model packageを実装する。
- ONNX checkerと実ONNX Runtimeでdynamic shapeを実行し、eagerとのparityを確認する。
- Raspberry Pi 5のbenchmarkはhardware検証として分離し、ユーザーが実機で実行する。

### Phase 5: 推論と塗布統合

- estimator公開API、manifest/checksum検証、棄却理由、集約・clampを実装する。
- 実ONNX Runtimeとlossless PNG fixtureを使って公開APIからend-to-end推論する。
- fake cameraのWebUI E2Eでpre/post撮影、推論結果表示、0件採用時fallbackを確認する。
- 実機では最後に撮影位置、1秒以内の推論、補正値が残りpadだけへ適用されることを確認する。

## 完了条件

- schema v1の複数sessionから、再現可能でleakのないsplitとbatchを作れる。
- 最小32 px、最大辺1024 px、最大262,144 pxの前処理契約と`pixel_per_mm`更新がtrain/inferenceで
    共通化されている。
- pure PyTorchだけでbase trainingとPi fine-tuningが動き、Pi fine-tuningが1時間以内に終わる。
- MLflowからdataset、config、code、metric、checkpoint、export、benchmarkのlineageを辿れる。
- 強制終了後に`latest.ckpt`から未処理batchを再開でき、不一致dataset/configを拒否する。
- promoted artifactが精度、coverage、export parity、Pi p95 1秒の全gateを通る。
- model不在、破損、範囲外、低信頼度、推論失敗時に`rotations_per_ul`を変更しない。
- 有効な推定が1個以上ある場合だけ、既存要件の式と1/3〜3倍clampで補正する。

## 参考資料

- [PyTorch: Start Locally](https://pytorch.org/get-started/locally/)
- [PyTorch: Saving and Loading Models](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html)
- [PyTorch: torch.export-based ONNX Exporter](https://docs.pytorch.org/docs/stable/onnx)
- [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/)
- [MLflow Tracking Server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)
- [ONNX Runtime Python](https://onnxruntime.ai/docs/get-started/with-python.html)
- [ONNX Runtime Quantization](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
