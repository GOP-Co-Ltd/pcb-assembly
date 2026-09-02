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

- すべての物理的な塗布方式について塗布体積を推定する。
- はんだ塗布ジョブの最初の有効な複数パッドを使ってキャリブレーションする。
- パージ対象パッドをキャリブレーションから除外する。
- 1 組の塗布前後画像だけでも体積を推定できる。
- 複数視点の画像を将来利用できるデータ形式にする。
- 別機体で収集したデータを使い、Raspberry Pi 5 上でモデルをファインチューニングできる。
- 従来の質量キャリブレーションを教師データの基準およびフォールバックとして残す。

## 対象範囲

- `dot`、`line`、`area` のすべての物理的な塗布方式を対象とする。
- `auto` は解決後の物理的な塗布方式として扱う。
- ノズル径、指令塗布量、吐出 rate にハードコードされた制限を設けない。
- システムは任意のノズル径、指令塗布量、吐出 rate のデータを保持・処理できる。
- 推定精度は、ベースモデルまたは対象機体でのファインチューニングデータがカバーする分布内
    で保証する。未学習領域での無制限な外挿は保証しない。
- 初期実装では、塗布途中のサブ秒単位のフィードバック制御は行わない。

## 前提

- オーガースクリューの 1 回転当たり吐出量は、1 回の収集または 1 回の運転時
    キャリブレーションの間は一定とみなせる。
- 塗布前画像と塗布後画像は、同じパッドと同じ view の組として対応付けられる。
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
pre RGB + post RGB
        ↓
6-channel image
        ↓
padding を learnable mask pixel で置換
        ↓
CNN encoder
        ↓
Global Average Pooling
        ↓
Linear → ReLU
        ├─ mean head
        └─ logvar head
```

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

体積を非負にするため、mean head の最終出力には Softplus を適用する。`logvar` には数値
安定性のため上下限を設ける。学習には Gaussian negative log-likelihood を使用する。

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

初期モデルは、1 組の塗布前後画像から推定できることを必須とする。データセットは、将来
複数視点を同時入力するモデルを検討できるよう、1 パッドに複数の view を保存できる形式に
する。

各 view は次の情報を持つ。

- 塗布前画像
- 塗布後画像
- view 番号
- 基準撮影位置からの X/Y offset [mm]

複数視点モデルを実装する場合は、共有 CNN で各 view を処理し、Global Average Pooling 後の
特徴を集約する方式を候補とする。複数視点入力は初期達成条件には含めない。

## データ収集

### データ収集用基板

多様な条件を効率よく収集するため、専用の基板データを生成して使用できる。専用基板には、
すべての塗布方式、複数の形状、塗布量、rate を収集できるパッドを配置する。収集ジョブ自体は
専用基板に限定せず、選択中の任意のKiCad PCBを使用できる。ノズル径、塗布量、rate に固定の
対象範囲は設けず、収集条件を metadata に記録する。収集対象とは別に、収集開始時だけ使用する
パージパッドを指定する。パージパッドは学習 sample に使用しない。

初期実装では基準位置のcentral viewだけを撮影する。schemaは、将来カメラをX/Y方向へ移動して
同じパッドを複数回撮影できるよう、複数viewを保持できる。同一viewの塗布前後画像は、同じ
撮影位置に対応させる。

#### はんだペースト流量キャリブレーション基板の生成仕様

データ収集基板の生成ロジックは、用途を明確にするため
`pcbasm.pasting.paste_flow_calibration_board` に置く。PCB一般のキャリブレーション基板を
意味する曖昧な `pcbasm.pcb.calibration_board` や、公開名としての単独の
`calibration_board` は使用しない。

初期設定は次のとおりとする。

- 基板外形: 40 × 40 mm
- 外周余白: 1 mm
- パッド間余白: 1 mm
- 専用purge pad: 2 × 2 mm、基板左上の外周余白内側
- purge padと通常パターン領域の間隔: 1 mm

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

配置は常に自動最適配置とする。個々のパッドAABBについて複数の安定したサイズ順とMaxRectsの
評価方法を試して、使用領域の面積、高さ、幅の順で最小になる配置を採用する。パッド間には
設定した余白を必ず確保する。purge padは左上へ固定するが、
上端全幅の専用帯は確保しない。purge padと設定余白を矩形keepoutとして扱い、その右側と下側を
同じ配置領域としてMaxRectsへ渡す。

生成物では、抽出したパッド1個を持つfootprintを各配置位置に生成し、そのパッドの
F.Cu/F.Mask/F.Pasteを保持する。元footprint全体のsilkscreenは部品配置を意味してしまうため
複製しない。reference/value文字は非表示にし、通常パッドへ`PAD1`からの安定したreference、
専用purge padへ`PURGE`を割り当てる（内部のpad numberは`1`）。KiCad footprint rootは
`KICAD9_FOOTPRINT_DIR`で上書きでき、未指定時は`/usr/share/kicad/footprints`を使う。

WebUIの「はんだ塗布」タブに「はんだペースト流量キャリブレーション基板生成」を置く。
名称検索とパッド種の一括追加を提供し、設定変更時は抽出した実パッド形状から解決した
F.Cu/F.Pasteと角度をSVGで表示する。部品名は画像へ常時描画せず、各パッドへのhover時に
tooltipで表示する。設定は
`pcbasm-paste-flow-calibration-board.json`、KiCad基板は
`pcbasm-paste-flow-calibration-board.kicad_pcb`としてブラウザへ直接ダウンロードする。
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
  "kind": "paste_flow_calibration_board",
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

### 収集手順

データ収集では、最初の収集対象パッドをパージに使用しない。収集前に吐出量
キャリブレーションを実施し、未塗布基板をTAREしてから装置へ設置する。位置・高さ・padの
位置合わせ後に全padの塗布前画像を撮影し、専用パージパッドへパージしてから本塗布を行う。
追加の手動ローディングは行わない。

1. 現行の吐出量キャリブレーションを実施する。
2. 未塗布の基板を電子天秤でTAREする。
3. 同じ基板を装置へ設置し、位置、高さ、領域、pad中心を位置合わせする。
4. 各収集対象パッドの塗布前画像を撮影する。
5. 専用パージパッドへパージする。
6. 収集対象パッドへ通常塗布と同じroute・解決済みpad設定で本塗布する。
7. 各収集対象パッドの塗布後画像を撮影する。
8. TAREした電子天秤で、パージ分を含む増加質量を計測する。
9. 総質量をペースト密度で総体積へ変換する。
10. パージと各パッドで実行したスクリュー回転数に比例して総体積を配分する。
11. パージを除く画像、撮影条件、塗布条件、体積 label をデータセットとして出力する。

収集対象には選択中の任意のKiCad PCBを使用できる。パージ先は既存の「初回パージパッド」
設定を共用し、明示選択がある場合はそのTop padを使う。未選択時に限り、データセット収集では
designatorが`PURGE`である一意なTop padを自動選択する。通常のはんだ塗布で使う未選択時の
先頭pad自動選択は変更しない。任意PCBに`PURGE`がない場合は、基板ビューから`U1.1`のような
通常の一意pad IDを初回パージパッドとして選択できる。purge padは有効padの収集sampleとroute
から除外する。purgeが未知または一意でない、purge以外の収集対象がない、
`initial_purge_ul <= 0`のいずれかでは、装置を動かす前に失敗する。

撮影時はF.Paste polygonのAABBへ`crop_margin_mm`を加えた矩形で、無加工のRGB画像を切り出す。
同寸法の単チャネルmaskを別PNGへ保存し、F.Paste polygonを`mask_margin_mm`だけ外側へbufferした
領域を255、その外側を0とする。`mask_margin_mm`の既定値は0.1 mmとし、maskがcropから欠けない
よう`crop_margin_mm`以下に制限する。cropがcamera frameを越える場合はpaddingせず失敗する。
塗布前後は同じ撮影位置とcrop矩形を使用する。初期WebUIはoffset `(0, 0)` のview 0だけを撮影
するが、schemaは複数viewを保存できる。

パッド \(i\) の教師体積は次式で求める。

\[
V_i
=
\frac{m_{\mathrm{total}}}{\rho}
\frac{r_i}{r_{\mathrm{purge}} + \sum_j r_j}
\]

- \(m_{\mathrm{total}}\): パージ分を含むTARE後の増加質量 [mg]
- \(\rho\): ペースト密度 [mg/µL]
- \(r_{\mathrm{purge}}\): 専用パージパッドで実行したスクリュー回転数 [rev]
- \(r_i\): パッド \(i\) で実行したスクリュー回転数 [rev]

パージ回転数は総体積の配分に含めるが、パージパッドの画像と配分体積は学習 sample として
出力しない。

## Data Augmentation

画像に対する Data Augmentation は次に限定する。

- 回転
- 等方スケール

塗布体積の手掛かりとなる輝度や陰影を不必要に変えない。回転時は
`pixels_per_mm`を変更せず、スケール変更時は倍率に合わせて更新する。

## データセット形式

### ディレクトリ構造

1 回の収集を 1 session とし、`<基板名>-<収集時刻>`のディレクトリ名を使用する。基板名は
KiCad基板ファイルの拡張子を除いた名前、収集時刻はtimezoneと3桁のmillisecondを含む値とする。

永続保存先はリポジトリ直下の`data/paste-volume-datasets/`とする。生成sessionは同directoryの
`.gitignore`でGit管理から除外する。書き込み中は同root内の一時directoryを使用し、完成時に
atomic renameする。abortまたは失敗時は取得済みファイルを`*.incomplete`として保持する。
完成sessionは永続保存したまま、直近ジョブのartifactとしてZIPも生成する。

```text
data/paste-volume-datasets/
├── .gitignore
└── board-20260828T143052.123+0900/
    ├── metadata.json
    ├── pre/
    │   ├── 000001.00.png
    │   ├── 000001.01.png
    │   ├── 000002.00.png
    │   └── ...
    ├── post/
    │   ├── 000001.00.png
    │   ├── 000001.01.png
    │   ├── 000002.00.png
    │   └── ...
    └── mask/
        ├── 000001.00.png
        ├── 000001.01.png
        ├── 000002.00.png
        └── ...
```

画像ファイル名は `<pad番号>.<view番号>.png` とする。

- pad 番号は収集 session 内で一意なゼロ埋め番号とする。
- view 番号は同じ pad に対する撮影位置を表す。
- `pre/` と `post/` の同名ファイルを 1 組とする。
- `mask/`の同名ファイルを塗布前後で共有する。
- KiCAD 上の pad ID などの元識別子は `metadata.json` に保存する。
- 画像は lossless PNG で保存する。

### metadata.json

schema v1は次の階層を持つ。すべての階層で未知keyと暗黙の型変換を拒否する。
`paste.lot`は任意で、製造ロットを入力しなかった場合は`null`とする。

```json
{
  "kind": "pcbasm-paste-volume-dataset",
  "schema_version": 1,
  "created_at": "2026-08-28T14:30:52.123456+09:00",
  "machine": {"machine_id": "machine-1", "name": "Machine 1"},
  "board": {
    "filename": "board.kicad_pcb",
    "source_pcb": "/boards/board.kicad_pcb",
    "signature": "..."
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
    "max_fill_speed_mm_s": 2.0,
    "max_dispense_rate_ul_s": 5.0,
    "dispense_accel_ul_s2": 10.0,
    "retract_amount_ul": 10.0,
    "retract_rate_ul_s": 10.0,
    "initial_purge_ul": 0.1,
    "crop_margin_mm": 1.0,
    "mask_margin_mm": 0.1
  },
  "total": {
    "measured_mass_mg": 0.756,
    "measured_volume_ul": 0.2,
    "rotations": 8.42
  },
  "purge": {
    "pad_id": "PURGE",
    "source_pad_id": "PURGE.1",
    "execution": {
      "applied_mode": "dot",
      "path_length_mm": 0.0,
      "commanded_volume_ul": 0.1,
      "prime_extra_volume_ul": 0.0,
      "effective_rate_ul_s": 1.0,
      "rotations": 4.21
    },
    "measured_volume_ul": 0.1
  },
  "pads": [
    {
      "index": 1,
      "pad_id": "U1.1",
      "source_pad_id": "U1.1",
      "polygon": {
        "exterior": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]],
        "holes": []
      },
      "resolved": {
        "dispense_mode": "dot",
        "line_direction": "unconstrained",
        "paste_height": "auto",
        "ul_per_mm2": 0.05,
        "prime_extra_delay": 0.0,
        "bead_width_factor": 1.0,
        "overlap": 0.0,
        "boundary_margin": 0.0
      },
      "execution": {
        "applied_mode": "dot",
        "path_length_mm": 0.0,
        "commanded_volume_ul": 0.1,
        "prime_extra_volume_ul": 0.0,
        "effective_rate_ul_s": 1.0,
        "rotations": 4.21
      },
      "measured_volume_ul": 0.1,
      "views": [
        {
          "number": 0,
          "offset_x_mm": 0.0,
          "offset_y_mm": 0.0,
          "pixel_rect": [400, 200, 880, 520],
          "pre": "pre/000001.00.png",
          "post": "post/000001.00.png",
          "mask": "mask/000001.00.png"
        }
      ]
    }
  ]
}
```

`rotations_per_ul` は現行実装と同じrev/µL単位とし、`rotations_per_mm`は使用しない。
各executionの`rotations`は`commanded_volume_ul + prime_extra_volume_ul`へ
`rotations_per_ul`を掛けた正方向の指令回転数で、prime押し戻しと後続retractionの往復分は
含めない。

## インターフェイス設計

WebUIのデータ収集ジョブと、crop・mask・metadata・教師体積配分・永続化は
`pcbasm.pasting`が担当する。モデル学習・評価・export・推論は`src/ml/`へ分離し、
運転時キャリブレーションから同じ推論APIを利用する。

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

収集、教師体積の配分、`rotations_per_ul`の補正計算は`src/pcbasm/`、ML用データ読み込み、
画像前処理、モデル、学習、評価、export、推論は`src/ml/`へ集約する。WebUIとCLIは同じ公開APIを
呼び出し、計算やドメインルールを複製しない。

### CLI

装置を動かさない処理は CLI を正規インターフェイスとする。リポジトリ直下の `scripts/` へ
スクリプトを追加せず、`src/ml/`内のPython moduleとして実装する。Hydraがargvを所有する
学習・評価entrypointと、argparseによる運用CLIは混在させない。

CLI は最低限、次の操作を提供する。

```text
python -m ml.paste_volume.cli dataset validate <dataset>
python -m ml.paste_volume.cli dataset summarize <dataset>
python -m ml.paste_volume.train experiment=base data.manifest=<dataset-manifest>
python -m ml.paste_volume.train experiment=fine_tune \
    parent_base_run_id=<base-mlflow-run-id> checkpoint.initial_weights=<weights> \
    data.manifest=<dataset-manifest>
python -m ml.paste_volume.evaluate weights=<weights> \
    data.manifest=<dataset-manifest> data.split_manifest=<split-manifest>
python -m ml.paste_volume.cli optimize <onnx-model> \
    --calibration-data <dataset> --split-manifest <split-manifest> --output <directory>
python -m ml.paste_volume.cli benchmark <onnx-candidate> --model-format onnx-fp32 \
    --data <dataset> --split-manifest <split-manifest> --output <report> \
    --power-condition <condition> --cooling-condition <condition>
python -m ml.paste_volume.cli infer <model-package> <pre-image> <post-image> \
    --pixel-per-mm <value>
```

ベースモデル学習、Raspberry Pi 5 上のファインチューニング、評価、最適化は WebUI の通常
ジョブにしない。最大 1 時間のファインチューニングで装置ジョブを占有したり、WebAPI
process に学習負荷を持たせたりしないためである。

将来、非開発者向けに WebUI からファインチューニングを開始する必要が生じた場合は、WebAPI
process 内で直接学習せず、独立 process を起動・監視する薄い wrapper として追加する。

## WebUI

### データ収集ジョブ

WebUI のはんだ塗布タブへ、`paste_dataset_collection` データ収集ジョブを独立した feature
として追加する。`paste_solder` と同じ pad editor 付き workspace を再利用し、選択中基板の
SVG 表示、Top/Bottom 切替、有効 pad の順路・塗布パス計算、階層別・部品別・pad 別の塗布量
override 表を表示する。編集値と順路・塗布パスは既存の pad-config API を正とし、データ収集
ジョブも同じ解決済み設定を使用する。データセット画面の初回パージパッド欄では、基板設定が
未選択の場合だけ `自動 (PURGE)` と表示する。ここでの選択は通常のはんだ塗布画面と同じ基板設定
へ保存されるため、ジョブフォームに重複するパージパッド入力は設けない。

必須の`ペースト製品ID`にはメーカー名・製品名または社内管理用の品番を入力する。
`製造ロット`は任意とし、入力する場合は容器に記載されたロット番号を使う。同じ製品でもロット、
保管期間、開封後時間などで粘度や吐出量が変わり得るため、学習データを後から分類・追跡するための
metadataとして保存する。

job form、job console、prompt、preview、abort、artifact、操作権も既存実装を再利用する。
データ収集専用の JavaScript や CSS は追加せず、共通 pad editor を読み込む。

WebUI ジョブは次を担当する。

1. 吐出量キャリブレーション実施済みの確認
2. 未塗布基板のTAREと設置案内
3. 選択中基板の位置・高さ・pad位置合わせ
4. 塗布前画像の撮影
5. 専用パージパッドへのパージ
6. 各収集対象パッドへの塗布
7. 塗布後画像の撮影
8. 計測質量の入力
9. コア API を呼び出したデータセット生成
10. 収集進捗、データセット保存先、成果サマリの表示

装置操作の順序、prompt、progress、preview、abort checkpoint、artifact への変換だけを
`src/web/api/` のジョブへ置く。データ収集用基板の生成、体積 label の計算、データセットの
書き込みは `src/pcbasm/` の公開 API に委譲する。

`src/web/ui/` は backend が返すジョブ定義、状態、ログ、prompt、preview、artifact をそのまま
表示する。体積、教師 label、信頼度、補正係数を JavaScript で再計算しない。

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

### 性能・運用

- Raspberry Pi 5 上で、前処理済みの単一 view 1 sample を 1 秒以内に推論する。
- Raspberry Pi 5 向けの推論最適化を必ず行ったうえで性能を評価する。
- Raspberry Pi 5 上のファインチューニングを 1 時間以内に終了する。
- データ収集形式およびモデル処理に、塗布方式、ノズル径、塗布量、rate の固定制限を
    設けない。
- 1 個以上の有効な画像推定から補正値を算出できる。
- 運転時の既定値では最初の 3 個の有効パッドを使用する。
- パージ対象パッドを運転時キャリブレーションへ混入させない。
- データ収集前に吐出量キャリブレーションを実施し、追加の手動ローディングを行わない。
- データ収集用基板に専用パージパッドを設け、収集対象パッドをパージに使用しない。
- 総質量に含まれるパージ回転数を教師体積の配分へ含め、パージパッド自体は学習 sample から
    除外する。
- 1 回の補正後の `rotations_per_ul` は補正前の 1/3 以上 3 倍以下とする。
- 推定不能または信頼度不足の場合は自動補正せず、従来の `rotations_per_ul` で続行または
    ジョブを中止できる。
- 別機体で収集したデータを使い、Raspberry Pi 5 上でファインチューニングできる。

## 未確定事項

- 自動補正に採用する `std / mean` の閾値
- Raspberry Pi 5 上で更新する CNN layer の範囲
- Raspberry Pi 5 向け推論 artifact の形式と最適化手法
- 複数視点モデルを導入する条件と特徴集約方式
