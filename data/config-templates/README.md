# マシン設定テンプレート

新規セットアップ用のマシン設定の初期値を置く。`./scripts/setup-machine-config.sh` がここから
`config/` を作る。

## 設定ファイルの置き場所と役割

マシン設定は 4 か所に分かれている。どれが「正」かを間違えないこと。

| 場所                                       | 役割                                                         | git        |
| ------------------------------------------ | ------------------------------------------------------------ | ---------- |
| `config/`（リポジトリルート）              | **実機の稼働設定。WebUI とジョブが読み書きする正**           | 追跡しない |
| `data/config-templates/<マシン名>.<用途>/` | 新規セットアップ用の初期値スナップショット                   | 追跡する   |
| `data/testing/config/`                     | WebUI / E2E 用フィクスチャ（Klipper port 7126 = 非リッスン） | 追跡する   |
| `data/testing/machine.toml`                | pcbasm コア層の単体テスト用フィクスチャ                      | 追跡する   |
| `~/printer_data/config/printer.cfg`        | **Klipper の正。`SAVE_CONFIG` もここに書く**                 | 追跡しない |

テスト用の `machine.toml` が 2 系統あるのは用途が違うため。`data/testing/config/machine.toml`（79 行）は
「マシン設定ディレクトリ」として丸ごと `tmp_path` に複製され、WebUI / E2E が読み書きする。コメント保持
（tomlkit）や欠落キー → `None` の検証素材を意図的に含む。`data/testing/machine.toml`（47 行）と
`machine_minimal.toml` は `pcbasm.config.Machine` の単体テストがパス指定で直接読むだけのフィクスチャ。

`config/` を追跡しないのは、WebUI が実測値（カメラキャリブレーション結果・ツールヘッドオフセット・
ノズルキャップ座標など）を常時書き込むため。追跡すると装置を動かすたびにリポジトリが dirty になる。

## テンプレートの構成

```
data/config-templates/
└── <マシン名>.<用途>/
    ├── machine.toml  # Klipper 以外のハードウェア設定
    └── printer.cfg   # Klipper 設定
```

`machine.toml` と `printer.cfg` の両方を持つディレクトリだけが
`./scripts/setup-machine-config.sh` の選択肢に出る。

**カメラキャリブレーション結果はテンプレートに含めない。** 機体固有の実測値であり、
別の機体に配ると誤った `pixel_per_mm` で動くことになる。セットアップ後に WebUI の
camera_calibration ジョブを実行し、Apply で `config/` に生成させる。

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

`<マシン名>.<用途>` — 用途は `machine.toml` の `machine_type`（`paste` / `pnp`）に一致させる。

- マシン名は小文字のスネークケース（例: `kurousagi`）
- 例: `kurousagi.paste`

## セットアップ

```sh
./scripts/setup-machine-config.sh
sudo systemctl restart klipper
```

スクリプトは実行内容を表示してから確認を取り、以下を行う。

1. `config/` に `machine.toml` を配置する
2. `printer.cfg` を `~/printer_data/config/printer.cfg` へ実ファイルとして配置する
3. 2 で配置した `printer.cfg` の `[mcu] serial` を実機の `/dev/serial/by-id/*` に書き換える
4. `config/printer.cfg` に 2 へのシンボリックリンクを張る（リポジトリから閲覧するため）
5. `klipper.env` の `KLIPPER_ARGS` を 2 のパスに向ける

4 と 5 は毎回張り直す（何度実行しても同じ結果）。

**既存の `config/machine.toml` は常に保持される（上書きしない）。** WebUI が実測値を書き込む
正であり、上書きすると操作者の設定が黙って巻き戻る。テンプレートから作り直したい場合は
退避してから再実行する。

```sh
mv config config.bak && ./scripts/setup-machine-config.sh
```

**既存の `~/printer_data/config/printer.cfg` は退避のうえ上書きするかを確認する（既定は上書き）。**
本スクリプトは Klipper インストール直後に走らせるのが通常の使い方で、そこには
`kinematics: none` の stub が既に置かれている。既定を保持にすると、テンプレートが永久に
反映されない。既存は `printer.cfg.bak.<日時>` へ退避されるため、取り違えても復旧できる。

稼働中の機体で `SAVE_CONFIG` の較正値（`load_cell_probe` の `counts_per_gram` や
`position_endstop`）が蓄積している場合は、確認プロンプトに `n` と答えて保持する。

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
mkdir data/config-templates/<マシン名>.<用途>
# machine.toml と printer.cfg を置く（キャリブレーション結果は含めない）
# printer.cfg の [mcu] serial は placeholder のままにする
git add data/config-templates/<マシン名>.<用途>
```

`./scripts/setup-machine-config.sh` の選択肢に自動で現れる。

## machine.toml の最小例

```toml
machine_type = "paste" # マシン種別: paste / pnp（必須）

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

書き込み可能なキーの全量は `src/web/api/config_store.py` の `MACHINE_FIELDS` を参照。

`[audio]`（通知音の出力デバイスと音量）は任意セクション。未設定なら ALSA の
システム既定デバイス・音量 0.75 で動く。WebUI の `/dev/audio` で選ぶと `config/machine.toml`
へ書き戻されるため、テンプレートではコメントアウトしてある。

## printer.cfg のバージョン管理について

Klipper は `~/printer_data/config/printer.cfg` を直接読み、`SAVE_CONFIG` の較正結果もそこへ
書き戻す。したがってリポジトリ側で printer.cfg を追跡することはできない（シンボリックリンク越しの
`SAVE_CONFIG` はリンクを壊すか、リポジトリを常時 dirty にするかのどちらかになる）。

`data/config-templates/<マシン名>.<用途>/printer.cfg` は
**セットアップ時点のスナップショットであり、実機とは drift する。これは意図した仕様。**

意味のあるチューニング（キネマティクス・ドライバ設定・`load_cell_probe` の較正など）を実機で
行ったら、テンプレートへ書き戻してコミットする。

```sh
cp ~/printer_data/config/printer.cfg data/config-templates/kurousagi.paste/printer.cfg
git diff data/config-templates/kurousagi.paste/printer.cfg  # 意図した差分か確認
```

書き戻すと `[mcu] serial` に実機の ID が入るため、placeholder
（`/dev/serial/by-id/<your-mcu-id>`）へ戻してからコミットする。

`#*# <---------------------- SAVE_CONFIG ---------------------->` 以降は Klipper が自動生成する
較正値ブロック。テンプレートに含めても含めなくてもよいが、含めるなら「どの実機のいつの値か」を
コミットメッセージに残す。

この書き戻しが頻繁で煩わしくなったら `scripts/setup-machine-config.sh` に snapshot サブコマンドを足す。
現時点では `cp` 1 行で足りるため用意していない。

カメラキャリブレーション結果の JSON も同様に `config/` に置かれ追跡されないが、こちらは
**テンプレートへ書き戻さない**（機体固有の実測値であり、別の機体に配ると誤った `pixel_per_mm`
で動くことになる）。機体を組み直したら再キャリブレーションする。
