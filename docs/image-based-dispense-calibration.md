# 画像ベース吐出量キャリブレーション要件

## 背景

現行のはんだペーストディスペンサーは、オーガースクリューの回転量を
`rotations_per_ul` で体積へ換算する。オーガースクリューは短期的には回転量に応じて
一定量を吐出するが、実効的な換算係数はペーストや装置の状態によって数時間から数日の
スパンで変化する。

現在は質量計測によって `rotations_per_ul` をキャリブレーションできるが、運転開始時の
実際の塗布結果からその日の状態へ追従する仕組みはない。電磁気的な流量計測も検討したが、
金属ノズル、ペーストの不安定な導電性、微小信号への対処が必要であり、絶対流量の計測手段
としては難易度が高い。

一方、塗布後画像は基板へ実際に付着したペーストの情報を含む。撮影条件と物理スケールを
明示したうえで CNN を使用し、塗布前後の画像から塗布体積を推定する。

## 目的

塗布前後の画像から実際に塗布されたはんだペーストの体積を µL 単位で推定し、その結果を
用いて実効的な `rotations_per_ul` を補正する。これにより、数時間から数日のスパンで生じる
流量変化へ運転開始時に追従し、その後の塗布量精度を維持する。

## 目標

- 点塗布（`dot`）の塗布体積を推定する。
- はんだ塗布ジョブの最初の有効な複数パッドを使ってキャリブレーションする。
- パージ対象パッドをキャリブレーションから除外する。
- 1 パッドを複数視点で撮影し、複数視点のまま体積を推定する。
- 1 view しか撮れない状況でも同じモデルで体積を推定できる。
- 別機体で収集したデータを使い、Raspberry Pi 5 上でモデルをファインチューニングできる。
- 従来の質量キャリブレーションを教師データの基準およびフォールバックとして残す。

## 対象範囲

- 物理的な塗布方式のうち `dot`（点塗布）だけを対象とする。`line` / `area` は収集も推定も
    行わない。運転時に補正するのは `rotations_per_ul` というグローバル係数なので、点塗布で
    得た比率を全方式へ適用できる。
- データ収集は素の銅板へのセル格子状の点塗布で行い、専用の KiCad 基板を使わない。
- ノズル径、指令塗布量、吐出 rate にハードコードされた制限を設けない。
- システムは任意のノズル径、指令塗布量、吐出 rate のデータを保持・処理できる。
- 推定精度は、ベースモデルまたは対象機体でのファインチューニングデータがカバーする分布内
    で保証する。未学習領域での無制限な外挿は保証しない。
- 初期実装では、塗布途中のサブ秒単位のフィードバック制御は行わない。

## 前提

- オーガースクリューの 1 回転当たり吐出量は、1 回の収集または 1 回の運転時
    キャリブレーションの間は一定とみなせる。
- 塗布前画像と塗布後画像は、同じセルと同じ view の組として対応付けられる。
- 画像の物理スケールは `pixels_per_mm` として取得できる。
- 質量から体積へ変換するため、使用するペーストの密度を取得できる。

## 運転時キャリブレーション

### 対象パッド

- 実際の塗布順から、パージ対象を除いた最初の `n` 個を使用する。
- `n` の既定値は 3 とする。
- `n` は 1 以上の整数として設定可能にする。
- 最小 1 個の有効な推定結果から補正できる。
- 初期パージを行ったパッドは、画像にパージ量と本来の塗布量が混在するため、`n` のカウント
    および補正値の計算から除外する。

### 処理手順

1. パージ対象を除いた最初の `n` 個のパッドを選択する。
2. 対象パッドの塗布前画像を撮影する。
3. 対象パッドへ通常どおり塗布する。
4. 対象パッドの塗布後画像を撮影する。
5. モデルで各パッドの塗布体積を推定する。
6. 有効な推定結果を集約し、指令体積に対する実塗布体積の比を求める。
7. `rotations_per_ul` を補正する。
8. 更新した値を残りのパッドへ適用する。

推定体積と指令体積の比を次のように定義する。

\[
g = \frac{\sum_i V_{\mathrm{estimated},i}}
         {\sum_i V_{\mathrm{command},i}}
\]

新しい換算係数は次式で求める。

\[
\mathrm{rotations\_per\_ul}_{\mathrm{new}}
=
\mathrm{rotations\_per\_ul}_{\mathrm{old}} \frac{1}{g}
\]

1 回の更新では、新しい `rotations_per_ul` を従来値の 1/3 以上 3 倍以下へ制限する。
推定値または不確かさが不正な結果は集約から除外し、有効な結果が 1 個も残らなければ
自動補正しない。

## モデル入出力

### 入力

モデルへ次を入力する。

- 塗布前画像
- 塗布後画像
- `pixels_per_mm`

塗布前後の RGB 画像はチャネル方向へ連結し、6 チャネルの画像として CNN へ入力する。
元画像の解像度とアスペクト比は任意とする。batch 内で大きさを揃えるために追加した
padding 位置は、入力チャネル数と同じ長さを持つ学習可能な mask pixel ベクトルで
置き換える。

```text
view ごとの pre RGB + post RGB
        ↓
6-channel image × V
        ↓
padding を learnable mask pixel で置換
        ↓
共有 CNN encoder（全 view で同じ重み）
        ↓
Global Average Pooling
        ↓
view 方向の平均（V → 1）
        ↓
Linear → ReLU
        ├─ mean head
        └─ logvar head
```

view が 1 枚のときは平均が恒等になるので、同じ経路が単視点でもそのまま通る。幾何
augmentation と有効画素 mask は 1 sample の全 view で共有し、標準化も view をまたいだ
1 組の統計で行う（view 間の明るさ差を残すため）。

padding mask は Masked Global Pooling には使用しない。通常の Global Average Pooling を
使用し、CNN が学習可能な mask pixel を識別する構成とする。

padding 面積への不要な依存を抑えるため、次を行う。

- 近い画像サイズとアスペクト比を同じ batch へまとめる。
- 学習時に複数の padding 率を経験させる。
- 同じ画像へ異なる padding を加えた場合の推定差を検証する。

`pixels_per_mm` は、画像から物理スケールを推測させず明示的に与える。Scale
augmentation を適用した場合は、画像と同じ倍率で `pixels_per_mm` を更新する。

元画像の大きさは制限しないが、Raspberry Pi 5 上の推論時間を保証するため、モデルへ
渡す前処理済み画像には画素数の上限を設けられるものとする。リサイズ後も
`pixels_per_mm`を更新する。

### 出力

モデルは塗布体積のガウス分布を表す次の 2 値を出力する。

- `mean_volume_ul`: 推定塗布体積の平均値 [µL]
- `log_variance_volume_ul2`: 推定塗布体積の対数分散 [µL² の対数]

標準偏差は次式で得る。

\[
\sigma_{\mathrm{volume}}
=
\sqrt{\exp(\mathrm{logvar})}
\]

体積を非負にするため、mean head の最終出力には ReLU を適用する。真値 0 の blank を
厳密な 0 として表現できる必要があるためで、Softplus は厳密な 0 を出せない。代償として
前活性が負へ落ちた sample は平均側の勾配が 0 になるので、平均線形層の bias は
`mean_bias_initial` で正の値から初期化し、飽和した sample の割合を診断として監視する。
`logvar` には数値安定性のため上下限を設ける。学習には Gaussian negative log-likelihood
を使用する。

`std / mean` を相対的な信頼度指標として使用する。ただし、予測された `std` は主として
学習分布内の不確かさを表すため、別機体や未学習条件に対する信頼度は対象機体のデータで
検証し、必要に応じてファインチューニングする。

## モデル実装・学習

- PyTorch で実装する。
- CNN encoder、Global Average Pooling、回帰 head から構成する。
- ベースモデルは RTX 4090 クラスの GPU で学習する。
- 学習データと評価データは画像単位で無作為分割せず、収集 session、機体、ペースト lot、
    ノズルなどの単位で分離する。
- Raspberry Pi 5 上で、別機体の収集データを用いたファインチューニングを実行できる。
- Raspberry Pi 5 上の 1 回のファインチューニングは 1 時間以内に終了する。
- データ量が多い場合は epoch 数または使用 sample 数を制限し、1 時間で必ず終了する。
- validation loss を監視し、early stopping を利用できるようにする。
- 学習用 checkpoint と最適化済み推論 artifact を分離する。
- ファインチューニング後、Raspberry Pi 5 向けの推論最適化を必ず行う。
- 最適化手法は固定せず、入力サイズ、モデル規模、量子化などを実測に基づいて選定する。

ファインチューニングの 1 時間には学習処理を含める。最適化済み推論 artifact の生成時間は
別枠とする。

## 複数視点

**1 パッドを複数視点で撮影し、複数視点のまま推定する。** データセットは 1 パッドに複数の
view を保存し、モデルは共有 CNN で各 view を符号化してから集約する。

各 view は次の情報を持つ。

- 塗布前画像
- 塗布後画像
- view 番号
- 基準撮影位置からの X/Y offset [mm]

集約は Global Average Pooling 後の特徴の**平均**とする。学習可能な parameter を持たない
ので view 数が変わっても退化せず、5 view で学習した graph をそのまま 1 view で実行できる。
既定の 5 view は中心 1 点と対称な 4 方向（[データ収集](#%E3%83%87%E3%83%BC%E3%82%BF%E5%8F%8E%E9%9B%86)で定義）で、view 間に
先験的な優劣が無いことも平均を選ぶ理由になる。

**view 数は学習時と推論時で一致しなくてよい。**

- 学習時は batch ごとに残す view 数をランダムに選んで間引く（view dropout）。view 数への
    過適合を避け、少ない view でも動くことを訓練時に経験させる
- 推論時は 1 view から 5 view までを選べる。撮影時間と精度のトレードオフを運用側が選ぶ
- `[B, V, C, H, W]` は batch 内で view 数が揃っている必要があるため、間引きは batch 単位で
    行い、view 数の違う sample は別 batch へ分ける

**達成条件は 1 view でも成立させる。** 5 view を前提にした精度だけを条件にすると、
1 view しか撮れない状況で運用できなくなるため。

## データ収集

### 収集用銅板とセル格子

収集は素の銅板だけで行い、KiCad PCB を選択しない。ジョブ開始時に外形だけの矩形銅板データを
生成して位置・高さの基準に使う。銅板には solder mask のダムが無いので、パッド形状ではなく
セル格子で塗布位置を決める。

- 銅板寸法（既定 40 × 40 mm）から外周余白（既定 2 mm）を四方引いた矩形を有効領域とする。
- 有効領域へ一辺 `cell_size_mm`（既定 2 mm）のセルを `cell_size_mm + cell_gap_mm`
    （既定 2 + 1 = 3 mm）ピッチで行優先に敷く。
- パージ領域は有効領域の左上へ `purge_cell_size_mm`（既定 2 mm）角で置き、これを
    `cell_gap_mm` 分広げた矩形と交差する格子セルは収集対象から除外する。
- 各セルへ 1 点だけ塗布する。塗布方式は `dot` 固定で、`line` / `area` は収集しない。
- 塗布高さはジョブパラメータの必須数値 [mm]（既定 0.2）とする。膜厚追従の `auto` は
    点塗布では意味を持たないため採らない。

### 吐出量スイープと配置

吐出量は下限・上限・分割数と 1 量あたりのサンプル数で決める。

- 吐出量列は `volume_min_ul`（既定 0.05）から `volume_max_ul`（既定 0.2）を
    `volume_divisions`（既定 5）点に等分した昇順列とする。`volume_min_ul == volume_max_ul`
    は量 1 種への縮退として許容する。
- 塗布サンプル数 = `volume_divisions × samples_per_volume`（既定 5 × 3 = 15）。
- これに blank セル（`blank_count`、既定 4）を加えたものが撮影対象セル数となる。blank は
    塗布せず塗布前後の画像だけを撮り、真値 0 の sample として保存する。回転数比の体積配分には
    含めない。移動中のドロールが板へ落ちていないかの検出も兼ねる。
- 実際に使うセルは、パージ除外後の格子から撮影対象セル数ぶんをシード付きで無作為抽出し、
    板全体へ散らす。先頭から順に詰めると、格子容量に対して撮影対象セル数が少ない既定設定では
    サンプルが板の上端数行に固まり、照明ムラ・板の反り・端部の反射が帯単位で共通に乗る。
- 吐出量と blank の割り当ては、抽出したセルへシード付きシャッフルで配る。板上の位置と吐出量の
    相関を切るためであり、実際に使ったシードを metadata に記録する。ジョブパラメータのシードが
    0 の場合は実行ごとに生成する。同じシードなら使用セルと割り当てが再現する。
- 撮影対象セル数が格子容量を超える場合は、装置を動かす前に失敗する。
- 吐出量の下限が指令回転数として小さすぎる（`volume_min_ul × rotations_per_ul` が最小指令
    回転数を下回る）場合も、装置を動かす前に失敗する。最小指令回転数は、ペーストスクリューの
    ステッパー（200 step/rev を 64 分割）の 1 マイクロステップぶんとする。

### 多視点撮影と crop

各セルは中心 view と周辺 view を撮影する。

- 周辺 view 数は `view_count`（既定 4）、移動距離は `view_offset_mm`（既定 1 mm）。
    周辺 view の角度は +X を 0 度として反時計回りに 360/`view_count` 度ずつ回す。
- `view_count = 0` は中心 view のみ。塗布前後は同じ view 番号どうしを 1 組とする。
- crop は一辺 `crop_size_mm`（既定 2 mm）の正方形で、セル寸法とは独立に設定する。隣セルの
    写り込みを防ぐため `crop_size_mm <= cell_size_mm + cell_gap_mm` を強制する。
- crop のピクセル寸法は収集開始時に `crop_size_mm × pixel_per_mm` から 1 回だけ決め、中心
    pixel が 1 つ存在するよう奇数へ寄せる。以降の全セル・全 view はこの寸法で切り出すため、
    1 session の全画像が同一ピクセル寸法になる。
- 塗布後のはんだ円径は事前に分からないため、mask 画像は保存しない。
- view offset を足しても crop が camera frame に収まるか、全（セル × view）の撮影位置と、
    パージ点・全塗布セルのノズル位置がステージ可動域に入るかは、収集の移動を始める前に検証する。
    塗布位置は撮影位置からツールヘッドオフセットぶんずれるので、撮影とは別に検証する。
- crop が camera frame を越える場合は padding せず失敗する。塗布前後は同じ計算で同じ crop
    矩形を得るため、板を動かさない限り pre / post の位置は一致する。

### 位置合わせ

銅板には照合対象の銅箔島パターンが無いので、通常のはんだ塗布で使う領域照合・pad 中心照合は
使わない。基準点計測で得た board 変換と、板外形を銅箔とみなして計測した高さ面だけを使う。
board → pixel の射影も board 変換・オフセット変換・`pixel_per_mm`・画像サイズだけから作る。

crop 位置の絶対精度は基準点と board 変換に由来するが、crop はセル中心を狙う固定寸法窓なので、
数十 µm の系統誤差は窓内で点がわずかに寄るだけで済む。

### 収集手順

収集前に吐出量キャリブレーションを実施し、未塗布の銅板をTAREしてから装置へ設置する。追加の
手動プライム・手動ローディングは行わない。この前提は装置の外から観測できないので、開始時の
確認プロンプトで運転者に確認させる。

1. セル格子・吐出量スイープ・view・crop 寸法を確定し、収まらない設定は装置を動かす前に失敗
    させる。点数と撮影枚数をログに出す。
2. 現行の吐出量キャリブレーションが済んでいること、その後に手動プライム・手動ローディングを
    行っていないことを確認する。
3. 未塗布の銅板を電子天秤でTAREし、同じ銅板を装置へ設置する。
4. 矩形銅板データを生成し、位置と高さを計測する。
5. 全セル（blank を含む）を全 view で塗布前撮影する。
6. パージ領域の中心へパージする。
7. blank を除く全セルへ点塗布する。
8. 全セル（blank を含む）を全 view で塗布後撮影する。
9. 計量質量以外を確定させた`pending.json`をsessionへ書く。
10. TAREした電子天秤で、パージ分を含む増加質量を計測する。入力待ちに入ったことは機体の
    スピーカーで知らせる（収集は1時間規模で、作業者は装置の前を離れている）。
11. 総質量をペースト密度で総体積へ変換する。
12. パージと各塗布セルで実行したスクリュー回転数に比例して総体積を配分する。blank セルは配分に
    含めず、真値 0 とする。
13. 画像、撮影条件、塗布条件、体積 label をデータセットとして出力する。

計量質量は収集で唯一、装置の外から来る値である。手順 9 でそれ以外を先に永続化するのは、
最後の入力にだけ依存して数十分ぶんの撮影を失わないようにするため。

撮影と塗布は「全点の塗布前撮影 → パージ → 全点の塗布 → 全点の塗布後撮影」の 3 パスへまとめ、
撮影と塗布の切り替えを減らして収集時間を詰める。分岐は設けない。撮影順序は metadata の
`config.capture_order` に記録する。

パージは塗布パスの先頭に置く。塗布前撮影の前に打つと、撮影のあいだにプライム状態が抜ける。
撮影パスではディスペンサーを有効化しない。AirPump を入れたまま数十分ヘッドを動かすと、
加圧されたノズルからペーストが垂れて塗布前画像と blank セルが汚れる。

3 パスでは先頭の点が塗布から塗布後撮影まで数十分空くため、照明・カメラの自動利得・熱ドリフトと
ペーストのスランプが塗布前後の差分に乗る。この誤差は blank セルと `samples[].order` の記録から
事後に検出する。

ジョブ先頭でのリトラクションは行わない。塗布シーケンスはリトラクション量をprimeしてから同量を
retractする自己完結型であり、手動ローディングを挟まない収集で先頭にリトラクションを入れると、
plungerがbaselineより引き込まれた状態で全点が走る。プライム状態のずれは最初のパージが吸収する。

prime後の追加遅延は 0 に固定する。追加遅延中の押し出し量は指令量に依らず一定なので、小さい量の
点でラベルと blob の対応が歪む。

セル \(i\) の教師体積は次式で求める。

\[
V_i
=
\frac{m_{\mathrm{total}}}{\rho}
\frac{r_i}{r_{\mathrm{purge}} + \sum_j r_j}
\]

- \(m_{\mathrm{total}}\): パージ分を含むTARE後の増加質量 [mg]
- \(\rho\): ペースト密度 [mg/µL]
- \(r_{\mathrm{purge}}\): パージで実行したスクリュー回転数 [rev]
- \(r_i\): セル \(i\) で実行したスクリュー回転数 [rev]

パージ回転数は総体積の配分に含めるが、パージ位置の画像と配分体積は学習 sample として出力しない。

### ラベルの構造的限界

総質量を回転数比で配分する方式では、点ごとのラベルが厳密に「指令量 × 単一係数」になり、点ごとの
実際のばらつきはラベル化されない。モデルは指令量に比例する量を学ぶ。運転時のグローバル係数の補正
には足りるが、点ごとのばらつきを画像から詰めることは原理的にできない。

この性質を扱うため、次の 3 点を行う（点ごとの個別計量は実施しない）。

- `label.kind = "rotation_allocated"` を記録し、将来の直接計量ラベルと区別できるようにする。
- blank セル（真値 0）を混ぜ、原点に独立な真値を 1 種入れる。
- `samples[].order`（塗布実行順）を記録し、セッション中の流量ドリフトを事後に検出できるようにする。

## テスト塗布基板

`dot` / `line` / `area` を実基板形状で試す用途のために、専用の基板データを生成できる。これは
**データ収集には使わない**（収集は素の銅板とセル格子で行う）。塗布パラメータの目視確認や
テスト塗布のための基板であり、生成ロジックは現状 `pcbasm.pasting.testboard` に置いている。

### テスト塗布基板の生成仕様

テスト塗布基板の生成ロジックは、PCB一般のキャリブレーション基板を意味する曖昧な
`pcbasm.pcb.calibration_board` や、公開名としての単独の `calibration_board` は使用しない。

初期設定は次のとおりとする。

- 基板外形: 40 × 40 mm
- 外周余白: 1 mm
- パッド間余白: 1 mm
- 専用purge pad: 2 × 2 mm、基板左上の外周余白内側
- purge padと通常パターン領域の間隔: 1 mm
- 流量計測パッド: 2 mm角 × 5個、purge padと同じ帯の右側

配置単位は部品全体ではなく、KiCad footprintから抽出した1種類のパッド形状とする。
たとえば0402のpad 1とpad 2が同一形状なら1種類へまとめ、その代表パッド1個だけを回転・
複製する。QFNの外周リード、中央exposed pad、F.Pasteだけの分割開口のように、レイヤーまたは
形状が異なるものは別々のパッド種として扱う。同一判定にはF.Cu/F.Mask/F.Pasteの実ポリゴン、
pad属性、drill形状を使用し、0/90/180/270度の回転で一致する形状を同一種へまとめる。

各パッド種から回転分割数 `n` と繰り返し数 `m` に従って個別のパッドを生成する。
同じパッド種もグループ化せず、各パッドを独立した矩形として配置する。回転範囲を
`theta` 度としたとき、回転index `i` の角度は次式とする。

\[
\phi_i = i \frac{\theta}{n}, \qquad 0 \leq i < n
\]

`0 < theta <= 360`、`n >= 1`、`m >= 1` とし、回転範囲のデフォルトは180度とする。
過大入力によるリソース枯渇を防ぐため、全パッド種の `n * m` 合計は10,000以下とする。
パッド間隔の計算には、各回転角における抽出パッドのF.Cu/F.Paste AABBを使う。
footprint anchorを調整して各AABBを配置結果の矩形へ一致させる。WebUIでは `n` と `m` を
それぞれ「回転分割数」「繰り返し数」と表示する。配置方向を指定するオプションは設けない。

使用可能な候補は固定カタログに限定せず、インストール済みKiCad 9 footprint rootにある
すべての `*.pretty/*.kicad_mod` とする。WebUIでlibrary名またはfootprint名を検索し、選択した
footprintをその場でパッド種へ分類し、未追加のパッド種を1回の操作ですべて追加する。すでに
設定にあるパッド種は重複させない。全footprintを起動時にpcbnewへ読み込まず、ファイル名の
検索indexだけを作り、選択されたfootprintだけを読み込む。

任意サイズパッドも追加できる。名称は省略可能で、省略時は形状、寸法、必要に応じて角丸半径
から、`長円（スロット） 1.5 × 0.5 mm`のような名称を自動設定する。任意パッドは
F.Cu/F.Mask/F.Pasteを持つSMDパッドとして生成し、次の形状を扱う。「長円（スロット）」は
ペースト開口の形状を表し、穴あきのthrough-hole slotは生成しない。

| 形状             | 寸法指定           |
| ---------------- | ------------------ |
| 円               | 直径               |
| 矩形             | 幅、高さ           |
| 角丸矩形         | 幅、高さ、角丸半径 |
| 長円（スロット） | 幅、高さ           |

検索語が空のときは、はんだペースト印刷で一般的な次の69 footprintを名称一覧へ表示する。
これらは候補を探しやすくするための代表寸法であり、一覧外のfootprintも名称検索で選択できる。
BGAなど通常のペースト印刷対象ではないpackageは一般候補へ含めない。

| 分類                | 一般候補                                                                                                                                                                                                                                                                                                                                                |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| チップ抵抗          | `R_0201_0603Metric`, `R_0402_1005Metric`, `R_0603_1608Metric`, `R_0805_2012Metric`, `R_1206_3216Metric`, `R_1210_3225Metric`, `R_2010_5025Metric`, `R_2512_6332Metric`                                                                                                                                                                                  |
| チップコンデンサ    | `C_0201_0603Metric`, `C_0402_1005Metric`, `C_0603_1608Metric`, `C_0805_2012Metric`, `C_1206_3216Metric`, `C_1210_3225Metric`, `C_1812_4532Metric`                                                                                                                                                                                                       |
| チップインダクタ    | `L_0201_0603Metric`, `L_0402_1005Metric`, `L_0603_1608Metric`, `L_0805_2012Metric`, `L_1206_3216Metric`, `L_1210_3225Metric`                                                                                                                                                                                                                            |
| チップヒューズ      | `Fuse_0402_1005Metric`, `Fuse_0603_1608Metric`, `Fuse_0805_2012Metric`, `Fuse_1206_3216Metric`                                                                                                                                                                                                                                                          |
| LED                 | `LED_0603_1608Metric`, `LED_0805_2012Metric`, `LED_1206_3216Metric`                                                                                                                                                                                                                                                                                     |
| ダイオード          | `D_SOD-523`, `D_SOD-323`, `D_SOD-123`, `D_SMA`, `D_SMB`, `D_SMC`, `D_MicroMELF`, `D_MiniMELF`                                                                                                                                                                                                                                                           |
| SOT / power package | `SOT-23`, `SOT-23-5`, `SOT-23-6`, `SOT-23-8`, `SOT-89-3`, `SOT-223-3_TabPin2`, `TO-252-3_TabPin2`, `TO-263-3_TabPin2`                                                                                                                                                                                                                                   |
| SOIC / TSSOP / SSOP | `SOIC-8_3.9x4.9mm_P1.27mm`, `SOIC-14_3.9x8.7mm_P1.27mm`, `SOIC-16_3.9x9.9mm_P1.27mm`, `TSSOP-8_3x3mm_P0.65mm`, `TSSOP-14_4.4x5mm_P0.65mm`, `TSSOP-16_4.4x5mm_P0.65mm`, `TSSOP-20_4.4x6.5mm_P0.65mm`, `TSSOP-24_4.4x7.8mm_P0.65mm`, `TSSOP-28_4.4x9.7mm_P0.65mm`, `SSOP-16_4.4x5.2mm_P0.65mm`, `SSOP-20_4.4x6.5mm_P0.65mm`, `SSOP-28_5.3x10.2mm_P0.65mm` |
| DFN / QFN           | `DFN-8-1EP_2x2mm_P0.5mm_EP0.6x1.2mm`, `QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm`, `QFN-24-1EP_4x4mm_P0.5mm_EP2.5x2.5mm`, `QFN-32-1EP_5x5mm_P0.5mm_EP3.1x3.1mm`, `QFN-48-1EP_7x7mm_P0.5mm_EP5.15x5.15mm`                                                                                                                                                    |
| LQFP                | `LQFP-32_7x7mm_P0.8mm`, `LQFP-48_7x7mm_P0.5mm`, `LQFP-64_10x10mm_P0.5mm`, `LQFP-100_14x14mm_P0.5mm`                                                                                                                                                                                                                                                     |
| 水晶                | `Crystal_SMD_2012-2Pin_2.0x1.2mm`, `Crystal_SMD_2520-4Pin_2.5x2.0mm`, `Crystal_SMD_3225-4Pin_3.2x2.5mm`, `Crystal_SMD_5032-4Pin_5.0x3.2mm`                                                                                                                                                                                                              |

初期レシピは次の6 footprintから抽出した代表パッド種を使用する。

| footprint library           | footprint         | 回転範囲 | 回転分割数 | 繰り返し数 |
| --------------------------- | ----------------- | -------: | ---------: | ---------: |
| `Resistor_SMD.pretty`       | R_0402_1005Metric |      180 |          4 |          3 |
| `Resistor_SMD.pretty`       | R_0603_1608Metric |      180 |          4 |          3 |
| `Resistor_SMD.pretty`       | R_0805_2012Metric |      180 |          4 |          3 |
| `Resistor_SMD.pretty`       | R_1206_3216Metric |      180 |          4 |          3 |
| `Package_TO_SOT_SMD.pretty` | SOT-23            |      180 |          4 |          2 |
| `Package_TO_SOT_SMD.pretty` | SOT-23-5          |      180 |          4 |          2 |

流量計測パッドは、データセット収集と同じ点塗布で吐出量を確かめるためのパッドである。
点塗布しか行わないので幅と高さを別に持たず、大きさだけを設定する正方形とする。
個数は0以上1,000以下の整数で、0なら配置しない。purge padの右へパッド間余白を挟んで
並べ、有効領域の右端に達したら行を下へ折り返す。折り返しても収まらない設定は
配置不能として理由を返す。

配置は常に自動最適配置とする。個々のパッドAABBについて複数の安定したサイズ順とMaxRectsの
評価方法を試して、使用領域の面積、高さ、幅の順で最小になる配置を採用する。パッド間には
設定した余白を必ず確保する。purge padは左上へ固定するが、
上端全幅の専用帯は確保しない。purge padと各流量計測パッドを設定余白ぶん広げた矩形を
keepoutとして扱い、その外側を配置領域としてMaxRectsへ渡す。

生成物では、抽出したパッド1個を持つfootprintを各配置位置に生成し、そのパッドの
F.Cu/F.Mask/F.Pasteを保持する。元footprint全体のsilkscreenは部品配置を意味してしまうため
複製しない。reference/value文字は非表示にし、通常パッドへ`PAD1`からの安定したreference、
専用purge padへ`PURGE`、流量計測パッドへ`FLOW1`からの連番を割り当てる
（内部のpad numberはいずれも`1`）。KiCad footprint rootは
`KICAD9_FOOTPRINT_DIR`で上書きでき、未指定時は`/usr/share/kicad/footprints`を使う。

WebUIの「はんだ塗布」タブに「テスト塗布基板生成」を置く。
名称検索とパッド種の一括追加を提供し、設定変更時は抽出した実パッド形状から解決した
F.Cu/F.Pasteと角度をSVGで表示する。部品名は画像へ常時描画せず、各パッドへのhover時に
tooltipで表示する。設定は
`pcbasm-paste-test-board.json`、KiCad基板は
`pcbasm-paste-test-board.kicad_pcb`としてブラウザへ直接ダウンロードする。
配置領域を超えた場合もpreview自体は消さず、全パッドの診断配置を表示する。有効な配置領域の
外へ出たパッド形状の部分だけを赤で重ね、基板生成は配置可能になるまで無効にする。
装置を動かさないため、生成と設定入出力にWebUIの操作権は要求しない。
編集中の設定は機体ごとにブラウザのlocalStorageへ自動保存し、ページの再読込時に復元する。
保存内容はサーバーのImport APIでschema検証・正規化してから画面へ反映し、古いschemaまたは
破損した保存内容は破棄して初期設定へ戻す。サーバー側には編集中の設定を保存しない。
初期化、パッド追加、Import、previewでは、サーバーが返す正規化済み設定と参照用catalogを
正とし、WebUI側でパッド種や設定の正規順を再導出しない。
表ではfootprint名を「名称」として表示する。長い名称とパッド種は末尾を省略表示し、hover時の
tooltipで完全な文字列を確認できる。名称とパッド種の見出しでは表示行を昇順・降順に
並べ替えられる。この表示順は設定の正規順や基板上の配置順を変更しない。名称検索の下では、
形状と寸法を入力して任意サイズパッドを追加できる。

設定JSONは自己識別情報を必須とする。Import時は`kind`と`schema_version`に加え、必須キー、
未知キー、値の型、ドメイン制約を厳密に検証し、文字列から数値などの暗黙変換は行わない。
妥当な設定はサーバーの安定順へ正規化する。配置不能な設定でも構造的に正しければExportできる。

```json
{
  "kind": "paste_test_board",
  "schema_version": 1,
  "board": {
    "width_mm": 40.0,
    "height_mm": 40.0,
    "edge_margin_mm": 1.0,
    "pad_gap_mm": 1.0
  },
  "purge_pad": {
    "width_mm": 2.0,
    "height_mm": 2.0
  },
  "flow_pads": {
    "size_mm": 2.0,
    "count": 5
  },
  "custom_pads": [
    {
      "catalog_id": "custom:0123456789abcdef0123456789abcdef",
      "name": "試験用長円",
      "shape": "oval",
      "width_mm": 1.5,
      "height_mm": 0.5,
      "corner_radius_mm": 0.0
    }
  ],
  "patterns": [
    {
      "catalog_id": "Resistor_SMD.pretty/R_0402_1005Metric#pad-0",
      "rotation_span_deg": 180.0,
      "rotation_count": 4,
      "repeat_count": 3
    },
    {
      "catalog_id": "custom:0123456789abcdef0123456789abcdef",
      "rotation_span_deg": 180.0,
      "rotation_count": 4,
      "repeat_count": 3
    }
  ]
}
```

## Data Augmentation

画像に対する Data Augmentation は次に限定する。

- 回転
- 等方スケール

塗布体積の手掛かりとなる輝度や陰影を不必要に変えない。回転時は
`pixels_per_mm`を変更せず、スケール変更時は倍率に合わせて更新する。

## データセット形式

### ディレクトリ構造

1 回の収集を 1 session とし、`<銅板名>-<収集時刻>`のディレクトリ名を使用する。銅板名は
`plate-<幅>x<高さ>`、収集時刻はtimezoneと3桁のmillisecondを含む値とする。

永続保存先はリポジトリ直下の`data/paste-volume-datasets/`とする。生成sessionは同directoryの
`.gitignore`でGit管理から除外する。書き込み中は同root内の一時directory（`.<stem>.tmp`）を
使用し、完成時にatomic renameする。abortまたは失敗時は取得済みファイルを`*.incomplete`として
保持する。完成sessionは永続保存したまま、直近ジョブのartifactとしてZIPも生成する。

未確定のsessionは計量質量だけが欠けた`pending.json`（schema v1）を持つ。これは`metadata.json`
から質量に依存する3つの値（`total`と、sampleおよびpurgeの`measured_volume_ul`）を抜いたもので、
計量値1つを与えれば完成`metadata.json`へ戻せる。確定したsessionは`pending.json`を持たない。

```text
data/paste-volume-datasets/
├── .gitignore
└── plate-40x40-20260908T143052.123+0900/
    ├── metadata.json
    ├── pre/
    │   ├── 000001.00.png
    │   ├── 000001.01.png
    │   ├── 000002.00.png
    │   └── ...
    └── post/
        ├── 000001.00.png
        ├── 000001.01.png
        ├── 000002.00.png
        └── ...
```

画像ファイル名は `<sample番号>.<view番号>.png` とする。

- sample 番号は収集 session 内で一意なゼロ埋め番号とし、塗布セルと blank セルで同じ採番列を
    共有する。
- view 番号は同じセルに対する撮影位置を表す。
- `pre/` と `post/` の同名ファイルを 1 組とする。
- mask は保存しない（塗布後のはんだ円径が事前に分からないため）。
- 1 session の全画像は同一ピクセル寸法（`config.crop_size_px` の正方形）である。
- 画像は lossless PNG で保存する。

### metadata.json

schema v2は次の階層を持つ。すべての階層で未知keyと暗黙の型変換を拒否する。
`paste.lot`は任意で、製造ロットを入力しなかった場合は`null`とする。

`schema_version`が2以外のdocumentは移行せず「未対応」として拒否する。v1（KiCad PCBのpad
polygonとmaskを前提とした版）からの移行関数は用意しない。

```json
{
  "kind": "pcbasm-paste-volume-dataset",
  "schema_version": 2,
  "created_at": "2026-09-08T14:30:52.123456+09:00",
  "machine": {"machine_id": "machine-1", "name": "Machine 1"},
  "plate": {
    "width_mm": 40.0,
    "height_mm": 40.0,
    "edge_margin_mm": 2.0,
    "height_plane_z_mm": -11.42
  },
  "paste": {"paste_id": "paste-1", "lot": "lot-1", "density_mg_per_ul": 3.78},
  "camera": {
    "pixel_per_mm": 120.5,
    "resolution": [1280, 720],
    "calibrated_at": "2026-08-20T12:00:00+09:00",
    "z_position_mm": 12.0
  },
  "nozzle": {"diameter_mm": 0.34},
  "config": {
    "rotations_per_ul": 42.1,
    "max_dispense_rate_ul_s": 5.0,
    "dispense_accel_ul_s2": 10.0,
    "retract_amount_ul": 10.0,
    "retract_rate_ul_s": 10.0,
    "initial_purge_ul": 0.2,
    "paste_height_mm": 0.2,
    "prime_extra_delay_s": 0.0,
    "cell_size_mm": 2.0,
    "cell_gap_mm": 1.0,
    "crop_size_mm": 2.0,
    "crop_size_px": 241,
    "purge_cell_size_mm": 2.0,
    "volume_min_ul": 0.05,
    "volume_max_ul": 0.2,
    "volume_divisions": 5,
    "samples_per_volume": 3,
    "blank_count": 4,
    "shuffle_seed": 20260908,
    "view_count": 4,
    "view_offset_mm": 1.0,
    "capture_order": "phased"
  },
  "label": {"kind": "rotation_allocated"},
  "total": {
    "measured_mass_mg": 0.756,
    "measured_volume_ul": 0.2,
    "rotations": 8.42
  },
  "purge": {
    "cell": {"x": 2.0, "y": 2.0, "width": 2.0, "height": 2.0},
    "center": {"x": 3.0, "y": 3.0},
    "execution": {
      "applied_mode": "dot",
      "path_length_mm": 0.0,
      "commanded_volume_ul": 0.2,
      "prime_extra_volume_ul": 0.0,
      "effective_rate_ul_s": 1.0,
      "rotations": 8.42
    },
    "measured_volume_ul": 0.1
  },
  "samples": [
    {
      "index": 1,
      "order": 1,
      "cell": {"x": 8.0, "y": 2.0, "width": 2.0, "height": 2.0},
      "center": {"x": 9.0, "y": 3.0},
      "commanded_volume_ul": 0.125,
      "volume_index": 2,
      "execution": {
        "applied_mode": "dot",
        "path_length_mm": 0.0,
        "commanded_volume_ul": 0.125,
        "prime_extra_volume_ul": 0.0,
        "effective_rate_ul_s": 1.0,
        "rotations": 5.26
      },
      "measured_volume_ul": 0.124,
      "views": [
        {
          "number": 0,
          "offset_x_mm": 0.0,
          "offset_y_mm": 0.0,
          "pixel_rect": [520, 240, 761, 481],
          "pre": "pre/000001.00.png",
          "post": "post/000001.00.png"
        }
      ]
    }
  ],
  "blanks": [
    {
      "index": 2,
      "cell": {"x": 11.0, "y": 2.0, "width": 2.0, "height": 2.0},
      "center": {"x": 12.0, "y": 3.0},
      "measured_volume_ul": 0.0,
      "views": [
        {
          "number": 0,
          "offset_x_mm": 0.0,
          "offset_y_mm": 0.0,
          "pixel_rect": [520, 240, 761, 481],
          "pre": "pre/000002.00.png",
          "post": "post/000002.00.png"
        }
      ]
    }
  ]
}
```

- `plate` は収集に使った銅板の寸法・外周余白と、計測した板面 Z。`camera.pixel_per_mm` は
    camera calibration 時の Z のものなので、板面 Z との差から実効スケールを補正できる。
- `cell` / `center` は銅板左上原点の board 座標 [mm]。
- `samples[].order` は塗布実行順（1 起点）で、セッション中の流量ドリフトを事後に検出するために
    記録する。`index` は画像ファイル名の番号と一致する。
- `blanks[]` は塗布しなかったセルで、`execution` を持たず `measured_volume_ul` は常に 0.0。
- `label.kind` は教師体積の作り方。`rotation_allocated` は「総質量を purge を含む指令回転数比で
    配分した」ラベルを表す。
- `config.capture_order` は撮影順序。現版は 3 パス固定で `"phased"` を記録する。点ごとの
    interleave で収集した既存 dataset を読めるよう、`"interleaved"` も受け付ける。

`rotations_per_ul` は現行実装と同じrev/µL単位とし、`rotations_per_mm`は使用しない。
各executionの`rotations`は`commanded_volume_ul + prime_extra_volume_ul`へ
`rotations_per_ul`を掛けた正方向の指令回転数で、prime押し戻しと後続retractionの往復分は
含めない。

## インターフェイス設計

今回の実装範囲は、WebUIのデータ収集ジョブと、crop・mask・metadata・教師体積配分・永続化を
担うコアAPIまでとする。以下のCLI、モデル学習・評価・推論、運転時キャリブレーションは
将来の実装範囲であり、今回のデータ収集機能には含めない。

実機操作と機械学習開発では必要なインターフェイスが異なるため、WebUI と CLI を次のように
使い分ける。

| 処理                                    | 主インターフェイス           |
| --------------------------------------- | ---------------------------- |
| 実機でのデータ収集                      | 最小限の WebUI ジョブ        |
| 運転開始時の画像キャリブレーション      | 既存のはんだ塗布ジョブへ統合 |
| データセットの検証・集計                | CLI                          |
| ベースモデルの学習                      | CLI                          |
| Raspberry Pi 5 上のファインチューニング | CLI                          |
| モデルの評価・最適化・benchmark         | CLI                          |

機械学習の責務は `src/ml/` に置く。画像前処理、モデル、学習、評価、export、そして塗布量推定の
ドメイン層 `ml.paste_volume` がここに入る。`src/pcbasm/` は装置の制御コアに徹し、データセットの
収集、教師体積の配分、`rotations_per_ul` の補正計算、`ml` が export した成果物を使う推論 API を
担う。WebUI と CLI は同じ公開 API を呼び出し、計算やドメインルールを複製しない。

配置と依存の向きの詳細は
[画像ベース吐出量推定 ML 実装計画](image-based-dispense-calibration-ml-plan.md) の
「7. 塗布フローで利用する module」を正典とする。

### CLI

装置を動かさない処理は CLI を正規インターフェイスとする。リポジトリ直下の `scripts/` へ
スクリプトを追加せず、`src/ml/`（機械学習）または `src/pcbasm/`（装置制御）内の Python
module として実装する。

CLI は最低限、次の操作を提供する。

```text
python -m ml.paste_volume.cli dataset validate <dataset>
python -m ml.paste_volume.cli dataset summarize <dataset>
python -m ml.paste_volume.cli train <dataset...>
python -m ml.paste_volume.cli finetune <checkpoint> <dataset>
python -m ml.paste_volume.cli evaluate <model> <dataset>
python -m ml.paste_volume.cli optimize <model>
python -m ml.paste_volume.cli benchmark <model>
python -m ml.paste_volume.cli infer <model> <pre-image> <post-image>
```

ベースモデル学習、Raspberry Pi 5 上のファインチューニング、評価、最適化は WebUI の通常
ジョブにしない。最大 1 時間のファインチューニングで装置ジョブを占有したり、WebAPI
process に学習負荷を持たせたりしないためである。

将来、非開発者向けに WebUI からファインチューニングを開始する必要が生じた場合は、WebAPI
process 内で直接学習せず、独立 process を起動・監視する薄い wrapper として追加する。

## WebUI

### データ収集ジョブ

WebUI のはんだ塗布タブへ、`paste_dataset_collection` データ収集ジョブを独立した feature
として追加する。収集は素の銅板で行うため PCB を選択せず、pad editor 付き workspace は使わない。
ページはジョブフォーム、配置プレビュー、カメラ preview、job console で構成する。初回パージ位置の
選択欄は持たない（パージ位置はセル格子から決まる）。

### 未完了datasetの確定ジョブ

撮影は終わったのに計量質量を入力できなかったsessionを確定させる`paste_dataset_finalize`
ジョブを同じタブへ置く。装置もPCBも使わない。パラメータは計量した増加質量だけで、対象session
は実行時のchoiceプロンプトで選ぶ（`pending.json`を持つ`*.incomplete`と`.<stem>.tmp`が候補）。
撮影済み画像は書き直さず、`metadata.json`を足してdirectoryを完成名へrenameする。

### 配置プレビュー

ジョブを始める前に点数と撮影枚数を確かめられるよう、ジョブフォームの下へ配置プレビューを置く。
フォームの値が変わるたびに `POST /api/pasting/paste-dataset-layout` を呼び、サーバーが返した
配置をそのまま描く。装置を動かさない読み取り専用計算なので操作権は要求しない（閲覧者にも
配置と枚数が読める）。

表示する内容は次のとおり。いずれもサーバーが算出した値をそのまま流す。

- 対象点数（塗布サンプル / blank の内訳）、1 セルあたりの view 数、撮影枚数
- 格子セル総数と、パージ領域を除いた配置可能セル数
- 配置シード（`0` のときは「実行時に生成」と示し、図は暫定配置であることを明示する）
- 銅板・有効領域・パージ領域・格子セル・使用セルの配置図（使用セルは吐出量で色分けし、
    blank は破線で区別する）

配置不能な設定でも図は消さず、理由を併記する（テスト塗布基板生成の preview と同方針）。
リクエストのキーはジョブの `ParamSpec` 名と一致させ、レイアウトに関係しない
`tolerance` / `paste_height` / `paste_id` / `paste_lot` は送らない。

ジョブフォームのパラメータは次のとおり。既定値はコア層の DTO 既定を唯一の出典とする。

| name                           | 表示                       | 既定              |
| ------------------------------ | -------------------------- | ----------------- |
| `plate_width` / `plate_height` | 銅板の幅 / 高さ [mm]       | 40.0              |
| `tolerance`                    | 位置合わせ許容誤差 [mm]    | 0.1               |
| `edge_margin`                  | 外周余白 [mm]              | 2.0               |
| `cell_size`                    | セル寸法 [mm]              | 2.0               |
| `cell_gap`                     | セル間隔 [mm]              | 1.0               |
| `crop_size`                    | 撮影 crop 寸法 [mm]        | 2.0               |
| `purge_cell_size`              | パージ領域寸法 [mm]        | 2.0               |
| `paste_height`                 | 塗布高さ [mm]              | 0.2（必須・正値） |
| `volume_min` / `volume_max`    | 吐出量の下限 / 上限 [uL]   | 0.05 / 0.2        |
| `volume_divisions`             | 吐出量の分割数             | 5                 |
| `samples_per_volume`           | 1 量あたりのサンプル数     | 3                 |
| `blank_count`                  | blank セル数               | 4                 |
| `view_count`                   | 周辺 view 数               | 4                 |
| `view_offset`                  | view の移動距離 [mm]       | 1.0               |
| `shuffle_seed`                 | 配置シード（0 で毎回生成） | 0                 |
| `paste_id`                     | ペースト製品ID             | 必須              |
| `paste_lot`                    | 製造ロット（任意）         | 任意              |

必須の`ペースト製品ID`にはメーカー名・製品名または社内管理用の品番を入力する。
`製造ロット`は任意とし、入力する場合は容器に記載されたロット番号を使う。同じ製品でもロット、
保管期間、開封後時間などで粘度や吐出量が変わり得るため、学習データを後から分類・追跡するための
metadataとして保存する。

job form、job console、prompt、preview、abort、artifact、操作権は既存実装を再利用する。

WebUI ジョブは次を担当する。

1. セル格子・吐出量スイープ・view・crop 寸法の確定と、収まらない設定の事前失敗
2. 吐出量キャリブレーション実施済みと手動プライム・手動ローディング未実施の確認
3. 未塗布銅板のTAREと設置案内
4. 矩形銅板の生成と位置・高さの計測
5. パージ領域中心へのパージ
6. セルごとの塗布前撮影 → 塗布 → 塗布後撮影
7. 計測質量の入力
8. コア API を呼び出したデータセット生成
9. 収集進捗、データセット保存先、成果サマリの表示

装置操作の順序、prompt、progress、preview、abort checkpoint、artifact への変換だけを
`src/web/api/` のジョブへ置く。セル格子・量割り当て・view 生成・事前検証・crop・体積 label の
計算・データセットの書き込みは `src/pcbasm/` の公開 API に委譲する。

`src/web/ui/` は backend が返すジョブ定義、状態、ログ、prompt、preview、artifact をそのまま
表示する。体積、教師 label、信頼度、補正係数、点数や撮影枚数を JavaScript で再導出しない。

### 運転時キャリブレーション

運転開始時の画像キャリブレーションは、新しい独立ページを作らず、既存の
`paste_solder` ジョブへ統合する。パージ対象を除く最初の `n` 個を撮影・推論し、補正後の
`rotations_per_ul` を残りのパッドへ適用する。

WebUI はキャリブレーションの進捗、使用 sample 数、推定体積、信頼度、補正前後の
`rotations_per_ul`、補正を見送った理由をジョブログと結果サマリに表示する。信頼度不足時に
ユーザー判断が必要な場合は、既存の prompt 機構を使用する。

### 初期実装の対象外

次のリッチな表示は、実運用で必要性が確認されるまで追加しない。

- データセット画像 gallery
- 学習曲線や体積分布の対話グラフ
- WebUI 内のモデル学習・checkpoint 管理画面
- frontend 側での推論、教師 label 計算、補正計算

## 達成条件

### 推定精度

真値に対する正規化誤差を次のように定義する。

\[
e_i
=
\frac{V_{\mathrm{estimated},i} - V_{\mathrm{true},i}}
     {V_{\mathrm{true},i}}
\]

評価データに対して次を満たす。

\[
\left|\operatorname{mean}(e)\right|
+
\operatorname{std}(e)
\leq
0.10
\]

これを「平均誤差から 1 sigma までを含めて ±10% 以内」の運用上の定義とする。

予測された不確かさについて、`mean ± 1 std` の区間が実測体積をおおむね 68.3% 包含する
ことを評価する。

**この精度条件は既定の 5 view で満たすことを必須とし、同じモデルが 1 view でも動作する
ことを併せて確認する。** 1 view の精度は 5 view を下回ってよいが、推論が成立しない
（実行できない、または誤差が発散する）状態は許容しない。view 数と精度の関係は評価
report へ残し、運用側が撮影時間とのトレードオフを選べるようにする。詳細は
[複数視点](#%E8%A4%87%E6%95%B0%E8%A6%96%E7%82%B9)。

### 性能・運用

- Raspberry Pi 5 上で、前処理済みの単一 view 1 sample を 1 秒以内に推論する。
- Raspberry Pi 5 向けの推論最適化を必ず行ったうえで性能を評価する。
- Raspberry Pi 5 上のファインチューニングを 1 時間以内に終了する。
- データ収集形式およびモデル処理に、ノズル径、塗布量、rate の固定制限を設けない。
- 1 個以上の有効な画像推定から補正値を算出できる。
- 運転時の既定値では最初の 3 個の有効パッドを使用する。
- パージ対象パッドを運転時キャリブレーションへ混入させない。
- データ収集前に吐出量キャリブレーションを実施し、追加の手動ローディングを行わない。
- データ収集は素の銅板のセル格子への点塗布で行い、専用の KiCad 基板を必要としない。
- 収集は塗布高さを必須の数値設定として受け取り、`auto`（膜厚追従）を使わない。
- 銅板の有効領域へパージ領域を設け、収集対象セルをパージに使用しない。
- 総質量に含まれるパージ回転数を教師体積の配分へ含め、パージ位置自体は学習 sample から
    除外する。
- blank セル（塗布しない真値 0 の sample）を収集へ含める。
- 1 session の全画像が同一ピクセル寸法であり、mask 画像を保存しない。
- 塗布前後の撮影は全点まとめた 3 パスで行い、撮影パスではディスペンサーを有効化しない。
- 1 回の補正後の `rotations_per_ul` は補正前の 1/3 以上 3 倍以下とする。
- 推定不能または信頼度不足の場合は自動補正せず、従来の `rotations_per_ul` で続行または
    ジョブを中止できる。
- 別機体で収集したデータを使い、Raspberry Pi 5 上でファインチューニングできる。

## 未確定事項

- 自動補正に採用する `std / mean` の閾値
- Raspberry Pi 5 上で更新する CNN layer の範囲
- Raspberry Pi 5 向け推論 artifact の形式と最適化手法
- 複数視点モデルを導入する条件と特徴集約方式
