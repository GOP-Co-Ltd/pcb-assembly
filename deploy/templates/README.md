# マシン設定テンプレート

新しい機体のマシン設定の初期値を機種ごとに置く。`./install.sh machine` がここから
`config/` と `printer.cfg` を作る。

## 設定ファイルの置き場所と役割

マシン設定は 4 か所に分かれている。どれが「正」かを間違えないこと。

| 場所                                | 役割                                                         | git        |
| ----------------------------------- | ------------------------------------------------------------ | ---------- |
| `config/`（リポジトリルート）       | **実機の稼働設定。WebUI とジョブが読み書きする正**           | 追跡しない |
| `deploy/templates/<機種>/`          | 新しい機体の初期値（機種で共通の値だけ）                     | 追跡する   |
| `data/testing/config/`              | WebUI / E2E 用フィクスチャ（Klipper port 7126 = 非リッスン） | 追跡する   |
| `data/testing/machine.toml`         | pcbasm コア層の単体テスト用フィクスチャ                      | 追跡する   |
| `~/printer_data/config/printer.cfg` | **Klipper の正。`SAVE_CONFIG` もここに書く**                 | 追跡しない |

テスト用の `machine.toml` が 2 系統あるのは用途が違うため。`data/testing/config/machine.toml`（79 行）は
「マシン設定ディレクトリ」として丸ごと `tmp_path` に複製され、WebUI / E2E が読み書きする。コメント保持
（tomlkit）や欠落キー → `None` の検証素材を意図的に含む。`data/testing/machine.toml`（51 行）は
`pcbasm.config.load_machine` の単体テストがパス指定で直接読むだけのフィクスチャ。

`config/` を追跡しないのは、WebUI が実測値（カメラキャリブレーション結果・ツールヘッドオフセット・
ノズルキャップ座標など）を常時書き込むため。追跡すると装置を動かすたびにリポジトリが dirty になる。

## テンプレートの構成

```
deploy/templates/
├── usaremino/      # cartesian（001 と 002）
│   ├── machine.toml  # Klipper 以外のハードウェア設定
│   └── printer.cfg   # Klipper 設定
└── kurousagi/      # inversed_corexy
```

`machine.toml` と `printer.cfg` の両方を持つディレクトリだけが `./install.sh machine` の選択肢に出る。

**個体の実測値はテンプレートに含めない。** カメラキャリブレーション結果、ツールヘッドの実測オフセット、
ノズルキャップとクリーニングの位置、ロードセルの較正値（`counts_per_gram` / `reference_tare_counts`）、
`SAVE_CONFIG` のブロックは機体ごとに違い、別の機体に配ると誤った値で動く。テンプレートには設計値と出発値だけを書き、
セットアップ後に [立ち上げ](../../docs/commissioning.md) の順に WebUI で実測して `config/` へ書かせる。

`machine.toml` の `[camera] calibration_file` はファイル名のみで書く。設定ディレクトリからの
相対パスとして解決されるため、`config/` へコピーした後もそのまま動く。テンプレートでは
存在しないファイル名（`calibration.json`）を指しており、キャリブレーション実施までは
フォーカス Z 位置が未取得（`None`）になるだけで起動や映像表示には影響しない。

## `[settle]` と `[detection]` は実機で詰める

どちらも装置の動作に直接効く。既定値は保守的な出発点で、**機体ごとに実測で詰める前提**。
WebUI の設定ページからも編集できる。

| キー                             | 既定 | 効き方                                                                                                           |
| -------------------------------- | ---- | ---------------------------------------------------------------------------------------------------------------- |
| `settle.move_sec`                | 0.5  | ステージ移動後、撮影・計測に入るまでの待ち。短くするとタクトが縮むが、機構が揺れたまま撮ると検出がぶれる         |
| `settle.probe_sec`               | 0.0  | PROBE 実行後の待ち                                                                                               |
| `detection.sample_count`         | 10   | 1 観測で撮るフレーム数。平均のばらつきは 1/√n で下がり、撮影時間は n に比例する（30 fps なら 10 枚で約 0.33 秒） |
| `detection.minimum_sample_count` | 5    | 1 観測に必要な有効検出数。届かなければ観測は失敗する。基準点の検出が渋い機体では下げる                           |

`move_sec` は位置合わせ・基板計測・高さ計測・撮影の**すべての移動後の静定**で共用する。
分けるべき差が実測で出たらそのとき項目を足す。

`[detection]` は基準点の円（機械的なマーカー）とツールヘッドオフセットの塗布痕（濡れた円）の
**両方**に効く。観測が失敗するときは、成功時のログに出る標準偏差
（`円検出: n/N フレーム, 標準偏差 X=... Y=... mm`）を見て、フレーム数と下限のどちらを動かすか決める。

## 命名規則

ディレクトリ名は機種名（小文字のスネークケース。例: `kurousagi`）。

## セットアップ

```sh
./install.sh machine
sudo systemctl restart klipper
```

`machine` 段は以下を行う。何度実行しても同じ結果になる。

1. `config/machine.toml` が無ければ、選んだ機種のものを置く
2. `~/printer_data/config/printer.cfg` が無いか、KIAUH が置いた stub（`kinematics: none`）なら、
    `printer.cfg.bak.<日時>` へ退避してから選んだ機種のものを実ファイルとして置く
3. 2 で置いた `printer.cfg` の `[mcu] serial` を実機の `/dev/serial/by-id/*` に書き換える
4. `config/printer.cfg` に 2 へのシンボリックリンクを張る（リポジトリから閲覧するため）

**既存の `config/machine.toml` と、stub でない `printer.cfg` には触らない。** どちらも WebUI や
`SAVE_CONFIG` が実測値を書き込む正であり、上書きすると操作者の設定が黙って巻き戻る。
テンプレートから作り直したい場合は、そのファイルを退避してから再実行する。

```sh
mv config config.bak && ./install.sh machine
```

### `[mcu] serial` の自動設定

`[mcu] serial` は機体固有なので、テンプレートには placeholder
（`/dev/serial/by-id/<your-mcu-id>`）を書いておく。スクリプトが `/dev/serial/by-id/` を見て
実デバイスのパスへ書き換える。

- デバイスが 1 つだけならそれを使う
- 複数あれば番号で選ばせる
- 1 つも無ければ placeholder のまま残し、警告を出す（MCU に Klipper firmware が
    書き込まれていないか、USB が未接続）

書き換え対象は無名の `[mcu]` セクションの `serial:` 行だけで、`[mcu <名前>]` は触らない。
既存の `printer.cfg` を保持した場合も触らない。

## テンプレートの追加

```sh
mkdir deploy/templates/<機種>
# machine.toml と printer.cfg を置く（個体の実測値は含めない）
# printer.cfg の [mcu] serial は placeholder のままにする
git add deploy/templates/<機種>
```

`./install.sh machine` の選択肢に自動で現れる。

## machine.toml の最小例

```toml
[klipper]
host = "localhost"
port = 7125

[camera]
device_id = 0
width = 640
height = 480
fps = 30.0
format = "MJPG"

[camera.crop]
width = 400
height = 400
```

書き込み可能なキーの全量は WebUI の設定ページに並ぶ項目で、設定のスキーマ（`src/pcbasm/config/`）の metadata から `pcbasm.config.fields.setting_fields()` が作る。

`[audio]`（通知音の出力デバイスと音量）は任意セクション。未設定なら ALSA の
システム既定デバイス・音量 0.75 で動く。WebUI の `/dev/audio` で選ぶと `config/machine.toml`
へ書き戻されるため、テンプレートではコメントアウトしてある。

## printer.cfg のバージョン管理について

Klipper は `~/printer_data/config/printer.cfg` を直接読み、`SAVE_CONFIG` の較正結果もそこへ
書き戻す。したがってリポジトリ側で printer.cfg を追跡することはできない（シンボリックリンク越しの
`SAVE_CONFIG` はリンクを壊すか、リポジトリを常時 dirty にするかのどちらかになる）。

`deploy/templates/<機種>/printer.cfg` は
**セットアップ時点の出発点であり、実機とは drift する。これは意図した仕様。**

機種で共通のチューニング（キネマティクス・ドライバ設定など）を実機で詰めたら、その差分だけを
テンプレートへ書き戻してコミットする。`[mcu] serial`、ロードセルの較正値、`SAVE_CONFIG` のブロックは
個体の値なので書き戻さない（機体の `printer.cfg` はバックアップに実体が入る）。

カメラキャリブレーション結果の JSON も同様に `config/` に置かれ追跡されないが、こちらは
**テンプレートへ書き戻さない**（機体固有の実測値であり、別の機体に配ると誤った `pixel_per_mm`
で動くことになる）。機体を組み直したら再キャリブレーションする。
