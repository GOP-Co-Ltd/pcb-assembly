# マシン設定テンプレート

新規セットアップ用のマシン設定の初期値を置く。`./setup-machine-config.sh` がここから
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
`./setup-machine-config.sh` の選択肢に出る。

**カメラキャリブレーション結果はテンプレートに含めない。** 機体固有の実測値であり、
別の機体に配ると誤った `pixel_per_mm` で動くことになる。セットアップ後に WebUI の
camera_calibration ジョブを実行し、Apply で `config/` に生成させる。

`machine.toml` の `[camera] calibration_file` はファイル名のみで書く。設定ディレクトリからの
相対パスとして解決されるため、`config/` へコピーした後もそのまま動く。テンプレートでは
存在しないファイル名（`calibration.json`）を指しており、キャリブレーション実施までは
フォーカス Z 位置が未取得（`None`）になるだけで起動や映像表示には影響しない。

## 命名規則

`<マシン名>.<用途>` — 用途は `machine.toml` の `machine_type`（`paste` / `pnp`）に一致させる。

- マシン名は小文字のスネークケース（例: `kurousagi`）
- 例: `kurousagi.paste`

## セットアップ

```sh
./setup-machine-config.sh
sudo systemctl restart klipper
```

スクリプトは実行内容を表示してから 1 度だけ確認を取り、以下を行う。

1. `config/` に `machine.toml` を配置する
2. `printer.cfg` を `~/printer_data/config/printer.cfg` へ実ファイルとして配置する
3. `config/printer.cfg` に 2 へのシンボリックリンクを張る（リポジトリから閲覧するため）
4. `klipper.env` の `KLIPPER_ARGS` を 2 のパスに向ける

1 と 2 は既存ファイルがあればスキップする。3 と 4 は毎回張り直す（何度実行しても同じ結果）。

**既存の `config/machine.toml` と `~/printer_data/config/printer.cfg` は常に保持される（上書きしない）。**
どちらも実測値が蓄積する正であり（machine.toml は WebUI が書き、printer.cfg は Klipper の
`SAVE_CONFIG` が較正値を追記する）、上書きすると操作者の設定が黙って巻き戻る。
このためスクリプトは何度実行しても既存の設定を壊さない。

テンプレートから作り直したい場合は、対象を退避してから再実行する。

```sh
mv config config.bak && ./setup-machine-config.sh                    # machine.toml を作り直す
mv ~/printer_data/config/printer.cfg{,.bak} && ./setup-machine-config.sh  # printer.cfg を作り直す
```

## テンプレートの追加

```sh
mkdir data/config-templates/<マシン名>.<用途>
# machine.toml と printer.cfg を置く（キャリブレーション結果は含めない）
git add data/config-templates/<マシン名>.<用途>
```

`./setup-machine-config.sh` の選択肢に自動で現れる。

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

書き込み可能なキーの全量は `src/webui/config_store.py` の `MACHINE_FIELDS` を参照。

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

`#*# <---------------------- SAVE_CONFIG ---------------------->` 以降は Klipper が自動生成する
較正値ブロック。テンプレートに含めても含めなくてもよいが、含めるなら「どの実機のいつの値か」を
コミットメッセージに残す。

この書き戻しが頻繁で煩わしくなったら `setup-machine-config.sh` に snapshot サブコマンドを足す。
現時点では `cp` 1 行で足りるため用意していない。

カメラキャリブレーション結果の JSON も同様に `config/` に置かれ追跡されないが、こちらは
**テンプレートへ書き戻さない**（機体固有の実測値であり、別の機体に配ると誤った `pixel_per_mm`
で動くことになる）。機体を組み直したら再キャリブレーションする。
