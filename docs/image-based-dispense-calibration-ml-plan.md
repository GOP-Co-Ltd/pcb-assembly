# 画像ベース吐出量推定 ML 実装計画

## 位置付け

この文書は、[画像ベース吐出量キャリブレーション要件](image-based-dispense-calibration.md)
に定義されたデータセット収集の次段階として、データ読み込みからモデルを実際の塗布フローで
利用するまでの実装方針を固定する。

初期実装は単一 view の塗布前後画像から体積と不確かさを推定する。複数 view モデル、学習用
WebUI、分散学習、モデルregistryによる自動配布は対象外とする。

主要な判断は次のとおりとする。

| 項目          | 採用方針                                                                         |
| ------------- | -------------------------------------------------------------------------------- |
| 学習framework | pure PyTorch。PyTorch Lightningは使わない                                        |
| 画像pipeline  | torchvisionでRGBのCHW tensorとしてdecode・変換する                               |
| 設定・探索    | TOML層をHydraなしで合成し、Optunaを直接駆動してhyperparameterを探索する          |
| 実験管理      | MLflow Trackingへ明示的に記録する                                                |
| 入力標準化    | 各sampleの`[6, H, W]`全体へaffineなしの`SampleLayerNorm`を適用する               |
| encoder正規化 | batch統計を持たないGroupNormを使い、BatchNormは使わない                          |
| モデル        | 3段のdownsampling stem、small ResNet encoder、Global Average Pooling、2 head回帰 |
| 学習単位      | 1 session・1 pad・1 viewを一意に識別し、同じpadのviewは同じsplitへ置く           |
| dataset統合   | 複数datasetを原本コピーなしのcomposite manifestで再現可能に統合する              |
| 推論artifact  | ONNXを基準形式とし、ONNX RuntimeのFP32とINT8を実測比較する                       |
| Pi上の推論    | 精度gateを通った候補のうち、Raspberry Pi 5で最速のartifactを採用する             |
| 中断再開      | modelだけでなくoptimizer、sampler位置、乱数状態を含むcheckpointから再開する      |

## 0. 下準備

### 依存関係

ML依存は通常のWebAPI/UI実行環境へ無条件に入れず、`pyproject.toml` のdependency groupを
分ける。

- `ml-runtime`: `torch`、versionを揃えた`torchvision`、`onnxruntime`。同じtensor前処理とONNX推論に
    使用する。
- `ml-train`: `ml-runtime`に加えてMLflow client。学習、評価、ファインチューニングに使用する。
- `ml-hpo`: `ml-train`に加えて`optuna`。GPU workstationでのhyperparameter探索に使用する。
    Hydraは採用しないため`hydra-core`と`hydra-optuna-sweeper`は依存へ入れない。
- `ml-export`: `onnx`、`onnxscript`、`onnxruntime`。export、量子化、parity評価に使用する。
- 通常のruntime: Raspberry Pi 5でも前処理をtorchvisionへ統一するため`ml-runtime`を使う。
    CNN本体はONNX Runtimeで実行し、PyTorch eager modelはloadしない。
- `pytorch-lightning`と`clearml`は初期依存へ追加しない。

CUDA wheelとARM64 CPU wheelは配布元が異なり得るため、実装開始時に
[PyTorch公式install selector](https://pytorch.org/get-started/locally/)で、GPU workstationと
Raspberry Pi 5の双方に存在する同一minor versionを確認し、PyTorchとtorchvisionの対応する
versionを一緒にlockする。versionを文書中の固定値にはせず、実際に検証したversionを`uv.lock`と
MLflow runへ残す。

MLflowは学習loopの依存に直接埋め込まない。`ExperimentLogger` protocolとMLflow adapterの
境界を設け、モデル、loss、optimizer、checkpointはMLflowをimportしなくても動くようにする。
ただし正式なtrain / fine-tune entrypointではMLflow loggerを必須とし、接続不能なら学習開始前に
失敗させる。consoleだけへ黙ってfallbackしない。

### 開発環境の確認

依存追加後に、次を自動確認するsmoke commandを用意する。

1. Python、PyTorch、torchvision、Optuna、CUDA、cuDNN、ONNX Runtimeのversionを表示する。
2. GPU workstationでは`torch.cuda.is_available()`が真で、CUDA tensorの畳み込みと
    backwardが成功することを確認する。
3. Raspberry Pi 5ではCPUで同じforward/backwardを実行する。
4. 64 × 64と1024 × 256のdummy inputでeager modelがforwardできることを確認する。
5. `torchvision.io.decode_image(..., mode="RGB")`がlossless PNGをRGBのCHW tensorとして読み、
    controlled fixtureのchannel値が期待値と一致することを確認する。
6. wheel同梱のTOML層をcomposeし、strict converterでfrozen configへ落として同梱groupを表示する。
7. MLflow tracking serverへtest run、metric、artifactを記録して読み戻す。

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
- 同一画像内容または同一session IDの衝突がないこと

ML pipelineのPNG decodeには`torchvision.io.decode_image(path, mode="RGB")`を使う。戻り値は
RGB順の`uint8 [C, H, W]`であるため、OpenCV由来のBGR変換やHWCからCHWへの`permute`を挟まない。
maskはgrayscaleとしてdecodeする。収集・幾何処理で既存OpenCVを使う箇所とは境界を分け、
controlled PNG fixtureでRGB channel順を固定する。

### 複合データセット

複数のdataset root、session directory、既存composite manifestを1つの学習datasetとして扱えるように
する。画像や`metadata.json`を別directoryへコピーする物理mergeは行わず、解決済みsession一覧を
持つ`composite.json`を生成する。これにより原本を不変に保ち、同じsessionを複数の組み合わせで
再利用できる。

```json
{
  "kind": "pcbasm-paste-volume-composite-dataset",
  "schema_version": 1,
  "name": "base-2026-09",
  "sources": [
    {"source_id": "machine-a", "path": "/data/machine-a"},
    {"source_id": "machine-b", "path": "/data/machine-b"}
  ],
  "sessions": [
    {
      "session_id": "session-a",
      "session_fingerprint": "sha256:...",
      "source_ids": ["machine-a"],
      "locations": [
        {
          "source_id": "machine-a",
          "relative_path": "20260901T010203Z-session-a"
        }
      ]
    }
  ],
  "content_fingerprint": "sha256:...",
  "composite_fingerprint": "sha256:..."
}
```

`dataset merge`は入力を再帰的にflattenし、session検証後にmanifestをatomic saveする。dataset loaderは
nested manifestを辿らず、常にflatten済み`composite.json`だけを読む。物理pathは探索用であり、
fingerprintには含めない。同じ内容とsource割当なら、別mount pathや入力順でも同じfingerprintになる。

sessionごとにmetadataと全画像から`session_fingerprint`、画像集合だけから
`image_set_fingerprint`を計算し、merge時に次を適用する。

- 同じ`session_fingerprint`が複数sourceにある場合は1 sessionへdeduplicateし、全`source_id`とpathを
    aliasとしてmanifestへ残す。
- 同じ`session_id`でfingerprintが異なる場合はID衝突として失敗する。
- 同じ画像集合に異なるmetadataまたは教師値が付いている場合は、競合labelを自動選択せず失敗する。
- sessionの追加・削除・内容変更は新しいcomposite fingerprintになる。既存manifestをin-place更新せず、
    新しいfileとして生成する。

formalなtrain / fine-tune / evaluate runではversion管理した`data.manifest`の指定を推奨する。
`data.roots`を直接複数指定することも許容するが、run開始時に同じ処理でflatten済みmanifestを生成し、
MLflowへ必ず保存する。この場合の`source_id`は各rootのcontent fingerprintから機械的に作り、pathや
入力順へ依存させない。人が読めるsource名や複数mount間の対応が必要なら、事前に`dataset merge`で
明示する。`data.manifest`と`data.roots`の同時指定は禁止する。

### sample index

1個のviewを1個の`PasteVolumeSample`とし、原本を変更せず、次の情報を持つindexを生成する。

- `sample_id`: session fingerprint、pad index、view numberから作る安定ID。compositeへ他sessionを
    追加しても既存sample IDを変えない
- `source_ids`: merge元datasetを示す1個以上のID。deduplicate時は全aliasを保持する
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
なく、各収集sessionが同程度の寄与を持つ。source directoryの分割方法は任意なので、sourceごとの
自動weight補正は行わない。source別のsample数とmetricは診断reportとして出す。

### dataset fingerprintとsplit

単一rootも内部では1 sourceのcompositeとして扱う。`content_fingerprint`はcomposite schemaと重複除去
済みsession fingerprintをsortしてSHA-256化する。dataset fingerprintとして使う
`composite_fingerprint`は、content fingerprintにsort済み`source_id`とsession-source対応を加えて
SHA-256化する。絶対pathとmanifest上の順序は含めない。生成したcomposite manifest、sample index、
split manifestを保存し、MLflowへartifactとして記録する。

無作為な画像単位splitは禁止する。初期実装は次の評価を分ける。

1. **primary split**: sessionを最小groupとしてtrain / validation / testへ分ける。同一sessionと
    同一padは複数splitへ跨がせない。既定比率は70 / 15 / 15とし、seed固定で再生成可能にする。
2. **cross-machine report**: machine単位のleave-one-group-out評価を行う。
3. **cross-lot report**: `(paste_id, paste_lot)`単位のleave-one-group-out評価を行う。
4. **cross-nozzle report**: nozzle径単位のleave-one-group-out評価を行う。
5. **cross-source report**: compositeの`source_id`単位でmetricを集計し、merge元dataset間のdomain
    shiftを可視化する。deduplicateされたsessionは各aliasへ重複加算せず、辞書順で先頭のprimary
    sourceへだけ計上する。

対象groupが2種類未満のcross-group評価は、画像単位splitへfallbackせず「評価不能」と記録する。
test splitはモデル選択、early stopping、不確かさ補正、export方式選択、INT8 calibrationに
使用しない。候補選択をvalidationで完了してから、選ばれたartifactをtestで1回だけ最終評価する。
split manifestが既に与えられた場合は再分割せず、そのfingerprint一致を要求する。base trainingは
train / validation / testへ最低1 sessionずつ存在すること、fine-tuningはtrain / validationへ最低
1 sessionずつ存在することを要求し、不足時は学習開始前に失敗する。

sourceごとに作られたsplit manifestを後から単純連結しない。splitは重複除去後のcomposite
fingerprintに対して1回生成する。一度test評価に使ったcompositeへsourceを追加する場合は新しいdataset
versionとsplitを作り、旧test sessionをtrainへ移さない。旧splitを引き継ぐ機能を実装する場合も、
既存sessionのassignmentを固定し、新規sessionだけをgroup単位で割り当てる。

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

### tensor化とSampleLayerNorm

train split全体、batch、channelごとの統計は使用しない。1個のsampleを「同じpad・viewの
pre/post pairを連結した`[6, H, W]` tensor」と定義し、sampleごとに`C`、`H`、`W`の全軸を
LayerNorm相当に標準化する。これを本計画では`SampleLayerNorm`と呼ぶ。次の順序をtrain、
validation、test、実運転推論で共通化する。

1. pre/postをtorchvisionでRGBの`uint8 [3, H, W]`としてdecodeする。
2. 全要素が真の`sample_valid_mask [1, H, W]`を作る。torchvision transforms v2のfunctional APIで、
    同じ明示parameterの幾何変換をpre、post、pad geometry mask、`sample_valid_mask`へ適用する。
    画像はbilinear、maskはnearest-exactを使う。
3. `to_dtype(torch.float32, scale=True)`で`[0, 1]`へ変換する。
4. pre RGB、post RGBの順で連結し、`x [6, H, W]`とする。
5. augmentationや回転で生じたinvalid領域を除く全channel・全有効画素から、そのsample固有の
    scalar `mean_sample`と`variance_sample`を1組だけ算出する。
6. 全6 channelへ同じ値を使い、
    `x_normalized = (x - mean_sample) / sqrt(variance_sample + eps)`とする。
7. invalid領域を0へ戻し、batch collate後にモデルのlearnable padding pixelで置換する。

`SampleLayerNorm`は`affine=False`とし、入力標準化用の学習可能なweight/biasを持たせない。
全画素が有効なsampleでは、`torch.nn.functional.layer_norm(x, normalized_shape=x.shape, weight=None, bias=None, eps=1e-5)`と同じ契約にする。invalid領域がある場合だけ、同じ計算を
`sample_valid_mask`でmasked化する。分散はmaskを6 channelへbroadcastし、`correction=0`で有効要素を
母集団として計算する。`eps=1e-5`はpreprocess schemaへ保存する。

preとpostを別々に標準化したり、RGB channelごとに標準化したりしない。これによりpre/post間の
明るさ差とchannel間の相対関係を保持しつつ、dataset全体の分布へ前処理を依存させない。また
sample単位の処理はcollateより前に行い、batch内の他sampleやbatch paddingが結果へ影響しないように
する。画像shapeは動的なので、固定`normalized_shape`を持つ`nn.LayerNorm` moduleではなく、上記の
計算を行うpure functionとして実装する。

有効要素0、`variance_sample < 1e-12`、mean/varianceが非有限のsampleは情報不足として拒否する。
epsilonは有効なsampleの数値安定化だけに使い、定数画像を見かけ上通さない。mean/varianceは推論時も
sampleから計算できるため、checkpointにはdataset統計を保存しない。ただし再現性と診断用に各sampleの
mean/variance分布をreportする。

`pixel_per_mm`はdata point単位では標準化できないscalarなので、train統計でcenter/scaleせず、
`log(pixel_per_mm)`をそのままモデルへ渡す。教師体積もtrain中央値でscaleせず、µLの物理単位の
ままlossへ渡す。したがってmodelの入出力変換にtrain dataset由来の統計は存在しない。

収集済みのpad geometry maskは入力channelへ加えない。v1のモデル入力は要件どおり6 channelを
維持し、geometry maskはcrop検証と幾何augmentationの整合確認に使う。augmentation後の有効領域を示す
`sample_valid_mask`、およびこれとbatch paddingを合成した`valid_pixel_mask`とは別物である。

### Data Augmentation

要件どおり回転と等方scaleだけを行う。輝度、contrast、色、blur、noiseは初期実装で変更しない。

- 回転角は`[0, 360)`から一様に選び、pre/postへ同じbilinear変換、geometry maskへ同じ
    nearest-exact変換を適用する。
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
6. 最後の小batchも捨てない。encoderはGroupNormのためbatch size 1でも同じ正規化規則になる。

collate時はbatch内の最大高さ・幅を32の倍数へ切り上げ、trainingでは画像の配置位置をランダム、
evaluationでは中央にしてpaddingする。collateは各`sample_valid_mask`とbatch padding領域を合成し、
画像tensorと`valid_pixel_mask [B, 1, H, W]`を返す。モデル側でinvalid位置を学習可能な6 channel
pixel vectorへ置換するため、padding値自体に意味を持たせない。

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
`Conv2d -> GroupNorm -> ReLU`の順を基本とする。residual blockは標準的な2個の3 × 3
convolutionとskip connectionを持ち、shape変更時だけ`1 × 1 Conv2d -> GroupNorm` projectionを
使う。2個目のGroupNorm後にskipを加算し、最後にReLUを適用する。

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
置換する。このparameterは標準化後の0から初期化し、dataset統計は使わない。Masked Global
Poolingにはせず、通常のGlobal Average Poolingを使う。

pooling後の160次元特徴へ、dataset統計で標準化していない`log(pixel_per_mm)` 1値を連結する。その後
`Linear(161, 128) -> ReLU`を通し、独立したmean headとlog-variance headへ分ける。画像から
見かけの大きさを学びつつ、物理scaleを明示的に利用できる構成になる。

modelはtrain dataset由来のscaleを介さず、µLの物理単位で直接出力する。

```text
mean_volume_ul = Softplus(raw_mean)
log_variance_volume_ul2 = clamp(raw_logvar, -14, 5)
```

meanの上限は設けない。log-varianceの範囲は数値安定性だけを目的とし、体積の対応範囲は
dataset coverageと評価結果で表す。

### encoderのNormalization

pixel-budget batchではbatch sizeが画像形状に応じて1から32まで変わり、Pi fine-tuningは最大4で
ある。このためbatch内統計とrunning statisticsに依存するBatchNormは使わない。v1では全stemと
residual blockに`GroupNorm(num_groups=8, num_channels=C, eps=1e-5, affine=True)`を使う。採用する
全channel数24、32、48、96、160は8で割り切れる。
8 groupはbaselineの既定値であり、Optunaで1 / 4 / 8 groupを比較した場合は、選択値をmodel configと
manifestへ固定して別model versionとして扱う。

GroupNormは各sample内でchannelをgroupへ分け、各groupのchannel・空間軸からmean/varianceを
計算する。batch軸を集計せず、trainとevalで同じ計算になる。入力`[6, H, W]`全体へaffineなしで
1回適用する`SampleLayerNorm`とは別のencoder内部処理であり、learnableなchannelごとのaffine
parameterは有効にする。

比較した候補と判断は次のとおりとする。

| 候補                      | 判断                                                                       |
| ------------------------- | -------------------------------------------------------------------------- |
| BatchNorm / SyncBatchNorm | 小さく可変なbatchとrunning statisticsへ依存するため不採用                  |
| InstanceNorm              | channelごとに独立して統計を求め、channel間関係を弱めるため不採用           |
| GroupNorm                 | ResNetへ小さな変更で導入でき、batch非依存かつ動的H/Wを扱えるため採用       |
| LayerNorm / ConvNeXt型    | batch非依存で有力だが、NHWC変換やblock全体の再設計を伴うためv1では比較候補 |
| RMSNorm                   | centerを行わずCNNでの根拠とexport実績がGroupNormより弱いため初期候補外     |
| EvoNorm等の独自層         | PyTorch標準module、ONNX、量子化の検証面を増やすため初期候補外              |

GroupNormを無条件にexport可能とは仮定しない。実装直後に最小・最大・縦長・横長入力をONNXへ
exportし、ONNX checkerとONNX Runtime parityを通す。失敗時はBatchNormへ戻さず、まずexporterの
対応versionと標準operatorへのdecompositionを確認し、それでも解決しない場合だけConvNeXt型
LayerNormを同じvalidation splitで比較する。

### モデル規模とfine-tuning範囲

v1は約150万parameter以下を目安とし、512 × 512入力で1.5 GMAC以下であることを実装時に
計測する。parameter数やGMACが上限を超えた場合、精度比較なしにchannelやblockを増やさない。

base modelは全層を学習する。Raspberry Pi 5での既定fine-tuningは次だけを更新する。

- residual stage 3
- pooling後のLinearと2個のhead
- learnable padding pixel

stem、residual stage 1・2はfreezeする。GroupNormはrunning statisticsを持たないため、更新対象の
stage 3では通常どおり学習し、freezeしたstageのaffine parameterは他のparameterと一緒に
`requires_grad=False`にする。全層fine-tuningはGPU用の明示optionとし、Piの既定値にはしない。

## 3. 学習パイプライン

### framework方針

PyTorch Lightningは採用しない。この規模では、必要な機能はdevice転送、AMP、gradient
accumulation、validation、early stopping、checkpoint、loggingに限定され、pure PyTorchで
十分に見通せる。MLflowのvanilla PyTorch autologgingへも依存せず、metricとartifactを明示的に
記録する。

設定管理はHydraを採用しない。config group、version管理されたexperiment config、解決済み
configの保存という構成上の考え方は
[lightning-hydra-template](https://github.com/ashleve/lightning-hydra-template)から取り入れるが、
実体は標準ライブラリの`tomllib`とcattrsで組む。LightningのTrainer、callback、DataModule、
logger wrapper、任意の`_target_`を設定からinstantiateする仕組みも持ち込まない。

Hydraを外した理由は次の4点である。第1に、**それまでlockしていた`hydra-core` 1.3.6 +
`hydra-optuna-sweeper` 1.4.0.dev9の組み合わせが実測で壊れていた**。最小sweepが
`InstantiationException('Cannot instantiate config of type TPESampler')`で即落ちする。
sweeper dev9が`instantiate(sampler, _execution_whitelist_=...)`を呼ぶのに対し、この引数は
hydra-core 1.3.6に存在しない。第2に、動く組み合わせはhydra-core / omegaconfのdev releaseを
3〜4点同時に固定することになり、安定版sweeperは`optuna<3`を要求する。第3に、Hydraのmultirunは
`BasicLauncher`の逐次`for`ループなので並列化に寄与しない（真の並列には別途launcher pluginが
要り、それも安定版は2022年で止まっている）。第4に、「既定値はattrsにのみ置き、TOMLは差分だけ
書く」方針が`@package`、custom resolver、`job.num`注入をほぼ不要にしていた。

設定合成は`ml.config.composition.ConfigComposition`が担当する。層を順にmergeし（mappingは再帰、
listは置換）、`key=value` tokenの値を`tomllib`のscalar規則で解釈してから、
`ml.serialization.make_strict_converter`でfrozen attrsへ構造化する。未知keyと暗黙の型変換は
converterが拒否し、失敗は例外ではなく理由文字列で返す。学習loop、モデル、data、logging、
checkpointは設定合成層をimportしない。dataset、checkpoint、output pathは合成後に絶対pathへ
解決し、環境変数や現在directoryを学習coreから暗黙参照しない。

### packaged config構成

configはPython packageと一緒にinstallできる場所へ置き、`ml.config.packaged.PackagedConfiguration`
が所在を解決する。直下のdirectoryがgroup、その中の`*.toml`がoptionになる。

```text
src/ml/config/conf/
└── trainer/edge.toml

pcbasm/pasting/paste_volume/conf/
├── base.toml
├── data/paste_volume.toml
├── model/resnet_small.toml
├── trainer/gpu.toml
├── trainer/pi.toml
├── logger/mlflow.toml
├── experiment/base.toml
├── experiment/fine_tune.toml
└── hyperparameter_search/base_optuna.toml
```

base層をgroup層より先に積み、group層は差分だけを書く（既定値はattrs側にのみ置く）。
`data/paste_volume.toml`は相互排他的な`manifest`と`roots`を持つ。未知keyはstrict converterが
禁止し、既定値を持たない必須fieldはrun開始前に落ちる。全runで合成後のfrozen configのJSON、
CLI override、config fingerprintをMLflowへ保存する。

### Optunaによる探索

Optunaは`ml.tuning`から直接駆動し、学習coreへ直接埋め込まない。Hydra Optuna Sweeperは使わず、
`ml.tuning.runner.HyperparameterSearch`が1プロセス分のtrialを共有studyへ積む。並列化の実体は
「複数のOS processが1個のRDB studyを共有する」ことであり、processを起こすのは運用者の仕事とする。
探索した値は`ConfigComposition`の`key=value`上書きへ素通しし、探索runと単発runが同じ設定経路を
通るようにする。初期search spaceはlearning rate、weight decay、gradient accumulation、
GroupNorm group数`{1, 4, 8}`に限定し、encoderの深さやchannel数はbaseline確立前に探索しない。

- objectiveはtestを含まない最小validation NLLの単一目的とする。
- dataset fingerprintとsplit manifestを全trialで固定する。
- samplerはOptunaの既定（TPE）に任せ、`ml.tuning`側にsamplerやseedを渡す口は設けない。
    trialごとの再現性はdataset fingerprintとsplit manifestの固定で担保する。
- 1プロセスあたりの`trial_count`は既定20とし、1枚のGPUへtrialを重ねない。
    `trial_count`は残trial数へ減算せず常に積み増す（何processが合流するかrunnerは知らない）。
- HPO用trialは最大60 epoch、early stopping patience 10とし、各trialを個別のMLflow runにする。
- Optuna storageはlocal fileではなく、tracking serverと同じPostgreSQL server内の専用databaseを
    推奨する。初期の単一端末では絶対pathのSQLiteも許容する。
- `study_name`はmodel family、dataset fingerprint、search space fingerprintから作り、同じstorageと
    study nameで再実行して完了trialを再利用できるようにする。identityとsearch spaceの
    fingerprintが食い違うrun、および既存studyとdirectionが食い違うrunは拒否する。
- 最良configはそのtrial weightをそのまま採用せず、通常の200 epoch上限で3 seedを再学習し、
    validation metricの平均とばらつきを確認してから候補化する。

探索の起動そのものをcheckpointとして扱わない。中断済みtrialはそのtrialの`latest.pt`から単独で
再開でき、study全体はpersistent Optuna storageから追加trialを継続する。HPO結果は
`ml.tuning.study.StudyResults`のdocumentとして残し、study名、search space fingerprint、
credentialを除いたstorage URIを載せる。生のstorage URIは成果物にも理由文字列にも書かない。

### lossとmetric

学習lossはµL単位の体積に対するGaussian negative log-likelihoodとする。

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
- 相対誤差`e = (mean - target) / target`のmean、std、`abs(mean(e)) + std(e)`
    （実装名は`relative_error_mean`、`relative_error_standard_deviation`、
    `relative_error_score`）
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
| `torch.compile`       |                  ON、Inductor、default mode |        ON、Inductor、default mode |
| trainable layer       |                                        全層 | stage 3、MLP、head、padding pixel |

model選択はcalibration前のvalidation NLL最小を第一条件とし、同値ならvalidation MAEが小さい方を
採用する。test metricでcheckpointを選ばない。

### 1 epochの処理

1. modelをtrain modeにし、fine-tuning時はfreeze対象の`requires_grad=False`を確認する。
2. deterministic batch planを生成する。
3. forward、weighted NLL、backward、gradient accumulation、clip、optimizer stepを行う。
4. 非有限lossまたはgradientを検出したら、そのstepを無視せず緊急checkpointを保存して失敗する。
5. epoch末にvalidationを`inference_mode`で実行する。
6. scheduler、early stopping、best checkpointを更新する。
7. metricsと診断artifactをMLflowへ記録する。

CUDAでは`torch.amp.autocast`とGradScalerを使う。CPUではAMPを使わない。

`torch.compile`はbase training、fine-tuning、PyTorch評価で既定ONとする。eager modelをdeviceへ
移した後、`torch.compile(eager_model, backend="inductor", mode="default", fullgraph=False, dynamic=None)`で別のforward callableを作る。可変shapeはPyTorchのautomatic dynamic shapesへ
任せ、aspect/area bucketによってshape種類と再compileを抑える。全dimensionを最初からdynamicに
する`dynamic=True`は既定にしない。

- compile前にeagerで1 batchのforward/backward smoke testを行う。
- compile失敗やgraph breakを黙ってeagerへfallbackしない。失敗runとして記録し、必要な場合だけ
    `trainer.compile.enabled=false`を明示して再実行する。
- first-step時間、steady-state sample/秒、遭遇したbatch shape数をMLflowへ記録し、Piの1時間制限は
    compile時間も含める。
- optimizerはeager modelのparameterから作り、checkpointはeager modelの`state_dict`を保存する。
    compiled wrapperや`_orig_mod.` prefixを永続化しない。
- ONNX exportもcompile済みwrapperではなくeager modelを使う。
- torchvisionのdecodeやresize/rotateはcompile対象に含めない。

### 再現性

- Python、NumPy、PyTorch CPU/CUDAへ同じrun seedから派生したseedを設定する。
- augmentationはsample単位、batch順はepoch単位の派生seedを使う。
- 初期baselineではdeterministic algorithmを有効にする。未対応opがあれば自動解除せず失敗し、
    性能優先runだけ明示optionで解除する。
- git commit、dirty flag、dependency version、dataset fingerprint、split manifest、全configを
    run開始時に保存する。
- dirty worktreeでの学習は許容するが、diffをartifactとして必ず保存する。

### Raspberry Pi 5の時間制限

fine-tuning entrypointはwall-clock deadlineを持つ。既定は55分でoptimizer stepを停止し、残り5分で
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
- 画像制約、`SampleLayerNorm`の軸・epsilon・affine設定、augmentation範囲
- optimizer、scheduler、batch pixel budget、seed、precision、compile設定、time budget
- Python、PyTorch、torchvision、Optuna、CUDA/cuDNN、MLflow、ONNX/ORTのversion

**metric**

- epochごとのtrain/validation lossと全評価metric
- learning rate、epoch時間、sample/秒、peak GPU memoryまたはprocess RSS
- testとcross-groupごとの最終metric
- export parity、artifact size、cold/warm latency、p50/p95/p99

**artifact**

- 解決済みconfig、composite manifest、sample index、split manifest、dataset検証report
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
├── latest.pt     # 一定stepごとの再開点
├── best.pt       # validation NLLが最良の再開可能checkpoint
├── final.pt      # 正常終了時の再開可能checkpoint
└── weights.pt    # best modelのstate_dictとmodel/preprocess schema（再開用ではない）
```

`latest.pt`は5分または500 optimizer stepの早い方、および各epoch末に保存する。
`SIGINT` / `SIGTERM`を受けた場合は新しいbatchを開始せず、現在のoptimizer step境界で
`latest.pt`を保存して終了する。

すべて一時fileへ書いて`fsync`後に`os.replace`する。途中書き込みのfileを有効checkpointとして
見せない。`best.pt`を上書きする前に新fileの読み戻し検証を行う。

### 保存内容

- checkpoint schema version、run ID、作成時刻
- model構成と`state_dict`
- optimizer、scheduler、GradScalerの`state_dict`
- epoch、global optimizer step、次に読むbatch index
- early stoppingのbest valueとpatience counter
- Python、NumPy、PyTorch CPU、全CUDA deviceの乱数状態
- samplerのepoch seedと、そのepochの確定batch plan
- `SampleLayerNorm` schema、不確かさcalibration前後の状態
- dataset fingerprint、split fingerprint、解決済みconfig

[PyTorchのcheckpoint指針](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html)
に沿い、model全体のpickleではなく`state_dict`を保存する。loadは`weights_only=True`を基本とし、
repository側のmodel classとschema versionから復元する。

### resumeとfine-tuneの区別

上書きtokenの`resume.checkpoint=/abs/path/latest.pt`は同一runの継続である。dataset
fingerprint、split、model構成、optimizer configが完全一致しない場合は拒否する。batch planと
batch indexから次の未処理batchを再開し、
augmentationもsample派生seedで同一にする。

fine-tuningは新しいrunであり、base model weightと前処理schemaだけを読み、optimizer、scheduler、
early stopping、samplerは新規作成する。この2操作を同じconfig fieldで兼用しない。

## 6. export、最適化、評価

### artifactの段階

学習用checkpointを塗布フローから直接読まない。次の一方向pipelineで推論artifactを作る。

```text
best.pt
    -> weights.pt
    -> ONNX FP32
    -> ONNX Runtime optimized FP32
    -> ONNX Runtime static INT8 candidate
    -> 精度・parity・Pi benchmark
    -> promoted model package
```

ONNX exportは`torch.onnx.export(..., dynamo=True)`を使い、batch、高さ、幅をdynamic dimensionに
する。export対象はbatch 1用wrapperであり、入力は`SampleLayerNorm`適用済みの6 channel画像と変換前の
`pixel_per_mm`、出力は物理単位のmeanとlog-varianceとする。前処理そのものはmanifestに従う
Python runtime moduleへ残す。

GroupNormを含むeager modelをそのままexportし、Conv、GroupNormのdecomposition、ReLU、Add、
GlobalAveragePool、Linear、Softplus相当の標準operatorだけでgraphが構成されることをONNX modelの
検査で確認する。独自operatorやcustom runtime extensionは許可しない。

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

**2. `torch.compile` parity評価**

最小・最大・縦長・横長の各shapeと学習用batchでeagerとcompiled modelのforward、loss、gradientを
比較する。forwardはONNX parityと同じ許容誤差、lossとgradientはdtype別に定めた相対・絶対誤差を
満たすことを要求する。compileの初回時間とsteady-state throughputも記録する。

**3. padding invariance評価**

同じ画像へ0%、10%、25%、50%の追加paddingを異なる辺へ加える。各条件の予測差を測り、paddingに
よる悪化後もprimary accuracy gateを満たすことを要求する。悪化が1 percentage pointを超える
場合はpromotionせず、bucket幅またはpadding augmentationを見直す。

**4. export parity評価**

全評価sampleでPyTorch eagerとONNX FP32を比較する。meanとlog-varianceが有限で、meanが正、
FP32出力差が`max(1e-6 µL, eager meanの0.1%)`以内であることを要求する。dynamic shapeの最小、
最大、縦長、横長も個別に通す。

**5. INT8精度評価**

INT8もprimary accuracy gateを満たし、FP32に対する
`abs(mean(e)) + std(e)`の悪化が0.01以下、68.3% coverageの差が3 percentage point以下である
ことを要求する。quantization calibrationにはtrain sampleだけを使う。

**6. Raspberry Pi 5 benchmark**

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

manifestにはartifact schema version、model名、input/output契約、dynamic dimension、
`SampleLayerNorm` schema、画像制約、不確かさoffsetとthreshold、学習dataset/split fingerprint、MLflow run ID、
exporter/opset/ORT version、quantization方式、各fileのSHA-256を含める。

load時に全checksum、schema version、必要runtime versionを検証する。未知schemaや壊れたartifactを
推測で読み込まない。

## 7. 塗布フローで利用するmodule

### 配置と責務

実装はドメイン非依存のML基盤 `ml`（`src/ml/`）と、塗布ドメイン層
`pcbasm.pasting.paste_volume` に分ける。依存の向きは `pcbasm` → `ml` の一方向とし、`ml` から
`pcbasm` / `web` をimportしない。WebAPIやUIへ計算を持たせない点は変わらない。

```text
ml.serialization                         # 暗黙変換を許さないcattrs converter
ml.artifact                              # atomic書き込み、fingerprint、document、不変package
ml.data                                  # 画像前処理、pixel budget batch、group split
ml.model                                 # GroupNorm residual block、Gaussian head、loss
ml.evaluation                            # 回帰metric、不確かさcalibration、slice診断、compile parity
ml.experiment                            # ExperimentLogger（ABC）とMLflow adapter
ml.training                              # TrainingTask / TrainingData（ABC）、checkpoint、Trainer
ml.config                                # TOML層の合成境界とpackaged config group
ml.tuning                                # Optuna study identity、storage検証、lineage検証
ml.export                                # ONNX、quantization、parity、benchmark、runtime

pcbasm.pasting.dataset                   # 収集schemaと原本の読み書き（metadata / writer / recorder / capture）
pcbasm.pasting.paste_volume.data         # session validate、composite manifest、sample index
pcbasm.pasting.paste_volume.model        # 塗布量推定modelとfine-tune範囲
pcbasm.pasting.paste_volume.task         # ml.training.TrainingTask / TrainingData の実装
pcbasm.pasting.paste_volume.train        # argvを所有するtraining entrypoint
pcbasm.pasting.paste_volume.evaluate     # argvを所有するevaluation entrypoint
pcbasm.pasting.paste_volume.conf         # packaged config group（TOML）
pcbasm.pasting.paste_volume.release      # 精度gateとpromotion
pcbasm.pasting.paste_volume.inference    # manifest検証、runtime、公開prediction API
pcbasm.cli.paste_volume                   # experiment configを要らない運用CLI
```

通常の`import pcbasm.pasting`でtorch、torchvision、Optuna、MLflow、ONNX Runtimeをeager
importしない。学習entrypoint、運用CLI、model loaderを呼んだ時点で必要依存を読み、未installなら
必要なdependency groupを示す明確なerrorを返す。

`ml`側は逆に、torchを隠すための関数内importをしない。`ml-runtime`だけをinstallした
Raspberry Pi 5で推論経路が動くよう、MLflow / Optuna / ONNXを要求するのは
それぞれのadapter moduleに限る。`ml.config` / `ml.tuning.study`はどれも要求しない。

### 学習entrypointと運用CLI

学習entrypointとsubcommand parserに同じargvを処理させない。学習entrypointは
`group=option`と`key=value`だけを受け取るので、`pcbasm.cli.paste_volume train ...`の残り引数を
中継するadapterは作らない。学習・fine-tuning・評価はargv全体を所有する独立moduleとする。
`group=option`か`key=value`かは、`name`がconfig root直下のdirectoryとして実在するかで振り分ける。

```text
python -m pcbasm.pasting.paste_volume.train \
    experiment=base data.manifest=/abs/base-2026-09.composite.json

python -m pcbasm.pasting.paste_volume.train \
    experiment=fine_tune model.initial_weights=/abs/weights.pt \
    data.manifest=/abs/machine-a-fine-tune.composite.json

python -m pcbasm.pasting.paste_volume.search \
    experiment=base hyperparameter_search=base_optuna \
    data.manifest=/abs/base-2026-09.composite.json

python -m pcbasm.pasting.paste_volume.evaluate \
    checkpoint=/abs/best.pt data.manifest=/abs/base-2026-09.composite.json \
    split=validation
```

train/fine-tuneの別は`experiment` config groupで表し、独立したCLI parserやflag集合を持たせない。
resumeだけは`resume.checkpoint=/abs/latest.pt`、fine-tuning初期weightは
`model.initial_weights=/abs/weights.pt`とし、意味を分ける。`split=test`はさらに
`allow_frozen_test=true`を要求し、通常の学習完了処理やOptuna trialから自動実行しない。

dataset検証、export、最適化、benchmark、単発推論はexperiment configを必要としないため、
薄い運用CLIへ残す。

```text
python -m pcbasm.cli.paste_volume dataset merge \
    --source machine-a=/abs/dataset-a --source machine-b=/abs/dataset-b \
    --output /abs/base-2026-09.composite.json
python -m pcbasm.cli.paste_volume dataset validate <dataset...>
python -m pcbasm.cli.paste_volume dataset summarize <dataset...>
python -m pcbasm.cli.paste_volume export <checkpoint> --output <directory>
python -m pcbasm.cli.paste_volume optimize <onnx-model> --calibration-data <dataset...>
python -m pcbasm.cli.paste_volume benchmark <model-package>
python -m pcbasm.cli.paste_volume infer <model-package> <pre-image> <post-image> \
    --pixel-per-mm <value>
```

`dataset validate`と`summarize`は単一root、複数root、composite manifestを同じAPIで扱う。
`dataset merge`の`source_id`はmanifest内で一意とし、同じIDの上書きを拒否する。両entrypointは
同じ公開Python APIを呼び、composite解決、dataset fingerprint、split manifestの生成、前処理、評価を
重複実装しない。`optimize`は候補packageを作るだけでactive modelを切り替えず、promotionは評価
reportを検証する別の公開APIで行う。

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
torchvision前処理を共有する。camera由来のRGB HWC `ImageArray`は
`torchvision.transforms.v2.functional.to_image`でCHW tensorへ変換し、手書きの`permute`を公開APIへ
散在させない。入力shape、RGB、有限で正のscale、画像上限、`SampleLayerNorm`のmean/variance、model coverageを
検証する。
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

- `dataset merge/validate/summarize`、composite manifest、sample index、fingerprint、group splitを
    実装する。
- 異なる入力順・mount pathでcomposite fingerprintが一致し、完全重複は1 sessionになり、IDまたは
    label競合は失敗することを確認する。
- torchvision decode、`SampleLayerNorm`、size制約、paired augmentation、bucket batch
    sampler、learnable padding用maskまでを通す。
- controlled RGB PNGと実session fixtureでRGB順、CHW shape、pair/mask整合、重複、leakage拒否を
    確認する。
- all-valid入力は`F.layer_norm`と一致し、masked入力はinvalid要素を除いた参照計算と一致することを
    確認する。複数sampleを同じbatchまたは別batchで処理しても各sampleの結果が変わらず、
    channel別・dataset全体の統計を読んでいないことを公開preprocessor APIで確認する。

### Phase 2: modelとpure PyTorch training

- GroupNorm small ResNet、Gaussian NLL、metric、base/fine-tune loopを実装する。
- 数sampleを過学習できること、可変shape batch、padding parameterへgradientが流れることを確認する。
- eagerと`torch.compile`のforward/loss/gradientが許容誤差内で一致し、compiled wrapperを含まない
    checkpointから再開できることを確認する。
- 中断あり/なしで同じseedの最終weightとmetricが一致するcheckpoint resume testを行う。

### Phase 3: 設定合成、Optuna、MLflow

- packaged config group、train/evaluate entrypoint、TOMLのsearch space宣言を実装する。
- config compose、未知key拒否、path解決、single run、複数processの合流、persistent study再開を
    integration testで確認する。
- explicit logger、合成後のfrozen config、metric、artifact、failure記録を実装する。
- 実local MLflow serverを使うintegration testでrunとartifactを読み戻す。MLflow APIはmockしない。
- 学習entrypointと、`dataset merge/validate/summarize`を含む運用CLIを接続する。

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

- schema v1の複数datasetを原本コピーなしでmergeし、provenanceを保った再現可能なcomposite
    fingerprint、leakのないsplit、batchを作れる。
- 最小32 px、最大辺1024 px、最大262,144 pxの前処理契約と`pixel_per_mm`更新がtrain/inferenceで
    共通化されている。
- torchvisionでRGB CHWへ統一し、各pre/post pairの`[6, H, W]`全体へaffineなしの
    `SampleLayerNorm`を適用し、batch、channel別、train dataset由来の前処理統計を持たない。
- encoderがGroupNormだけを使い、batch size 1でもtrain/eval間でrunning statisticsへ依存しない。
- 合成後のfrozen configからpure Python training APIを実行でき、Optuna studyと全trialをMLflowから
    追跡できる。
- `torch.compile`が既定ONで、eager parity、compile時間、checkpoint、ONNX exportの境界が検証される。
- Lightningなしのpure PyTorch training coreでbase trainingとPi fine-tuningが動き、Pi fine-tuningが
    1時間以内に終わる。
- MLflowからdataset、config、code、metric、checkpoint、export、benchmarkのlineageを辿れる。
- 強制終了後に`latest.pt`から未処理batchを再開でき、不一致dataset/configを拒否する。
- promoted artifactが精度、coverage、export parity、Pi p95 1秒の全gateを通る。
- model不在、破損、範囲外、低信頼度、推論失敗時に`rotations_per_ul`を変更しない。
- 有効な推定が1個以上ある場合だけ、既存要件の式と1/3〜3倍clampで補正する。

## 参考資料

- [PyTorch: Start Locally](https://pytorch.org/get-started/locally/)
- [torchvision: decode_image](https://docs.pytorch.org/vision/stable/generated/torchvision.io.decode_image.html)
- [torchvision: Transforms v2](https://docs.pytorch.org/vision/stable/transforms.html)
- [PyTorch: LayerNorm](https://docs.pytorch.org/docs/stable/generated/torch.nn.LayerNorm.html)
- [PyTorch: GroupNorm](https://docs.pytorch.org/docs/stable/generated/torch.nn.GroupNorm.html)
- [Group Normalization paper](https://arxiv.org/abs/1803.08494)
- [PyTorch: torch.compile](https://docs.pytorch.org/docs/stable/generated/torch.compile.html)
- [PyTorch: Dynamic Shapes](https://docs.pytorch.org/docs/stable/user_guide/torch_compiler/torch.compiler_dynamic_shapes.html)
- [PyTorch: Saving and Loading Models](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html)
- [PyTorch: torch.export-based ONNX Exporter](https://docs.pytorch.org/docs/stable/onnx)
- [Lightning-Hydra-Template](https://github.com/ashleve/lightning-hydra-template)
- [Optuna: Distributed Optimization](https://optuna.readthedocs.io/en/stable/tutorial/10_key_features/004_distributed.html)
- [Python: tomllib](https://docs.python.org/3/library/tomllib.html)
- [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/)
- [MLflow Tracking Server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)
- [ONNX Runtime Python](https://onnxruntime.ai/docs/get-started/with-python.html)
- [ONNX Runtime Quantization](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
