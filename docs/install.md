# 導入

[ドキュメント一覧](../README.md) · 次: [立ち上げ](commissioning.md)

空の SD カードから、ブラウザで WebUI が開くまでの手順。
導入が済んだら [立ち上げ](commissioning.md) へ進む。

## 用意するもの

- Raspberry Pi 5 と、Raspberry Pi OS 64bit（Debian 13 trixie）を書いた SD カード
- 機体（Klipper の MCU、カメラ、ロードセルプローブ、ディスペンサー）
- 通知音を使うならスピーカー（I2S DAC / アンプか HDMI 音声出力）

OS 以外（KiCad、picamera2、uv、Klipper、Moonraker など）は `install.sh` が入れる。

## 手順

OS を入れ、ホスト名を機体ごとに一意な名前へ変える。ホスト名は WebUI の機体の ID（URL の `/m/<ホスト名>`）になる。

1. リポジトリを取得する。置き場所は `~/pcb-assembly` に限る（PCB の場所から基板の識別子を決めるため、機体間でそろえる）。

    ```sh
    git clone https://github.com/GOP-Co-Ltd/pcb-assembly.git ~/pcb-assembly
    cd ~/pcb-assembly
    ```

2. 全段を順に実行する。sudo は付けない（必要な操作は段の中で sudo する）。

    ```sh
    ./install.sh
    ```

    段は次の順に進む。どの段も再実行してよく、`./install.sh <段>` で 1 段だけやり直せる。

    | 段          | すること                                                                                                      |
    | ----------- | ------------------------------------------------------------------------------------------------------------- |
    | `preflight` | Debian 13 (trixie) aarch64、既定のままでないホスト名、clone の位置を確かめる                                  |
    | `packages`  | apt で KiCad、`python3-picamera2`、`git-lfs`、`alsa-utils` などを入れ、Git LFS の実体を取得する               |
    | `python`    | 版を固定した uv を入れ、`make setup` で `.venv` を作る（既にあれば作り直さず `uv sync` で同期だけする）       |
    | `klipper`   | KIAUH を対話式で起動する（Klipper が入っていれば飛ばす）。独自 kinematics（`inversed_corexy`）を symlink する |
    | `hardware`  | カメラ・スピーカーの boot overlay と Klipper MCU firmware を対話式で設定する（`deploy/hardware.sh`）          |
    | `machine`   | `deploy/templates/` から機種を選び、`config/machine.toml` と `~/printer_data/config/printer.cfg` を置く       |
    | `services`  | `pcbasm-api` / `pcbasm-ui` の systemd unit と、WebUI からの更新に使う sudoers を設置して起動する              |

    - KIAUH では Klipper、Moonraker、Mainsail を選んで入れる
    - カメラは CAMERA port 0 / 1 と driver を選ぶ。driver は `ov9281`（標準構成）、`imx708`、
        `imx477`、`imx296`、`imx219`、`ov5647`、overlay 名の手入力から選ぶ
    - スピーカーは `max98357a`（標準構成）、`hifiberry-dac`、`hifiberry-dacplus`、
        `iqaudio-dacplus`、`googlevoicehat-soundcard`、overlay 名の手入力、未設定から選ぶ
    - BTT SKR Pico v1.0 は画面の案内に従って BOOT jumper と RESET を操作し、
        `/media/$USER/RPI-RP2` volume へ同梱 UF2 firmware（`deploy/firmware/`）をコピーする
    - boot 設定は `/boot/firmware/config.txt`（旧 OS では `/boot/config.txt`）へ反映され、
        変更前の内容は同じ場所の `config.txt.pcbasm.bak` に保存される。管理する marker 内だけを再実行時に置換する
    - `machine` は、既にある `config/machine.toml` と、KIAUH が置いた stub（`kinematics: none`）でない
        `printer.cfg` には触らない。テンプレートから作り直すときは、そのファイルを退避してから再実行する
    - 全段実行の `services` は backend と frontend の両方を置く（同居機）。frontend だけのホストや、
        frontend を別のホストにまとめる構成は [ホストの追加](maintenance.md#%E3%83%9B%E3%82%B9%E3%83%88%E3%81%AE%E8%BF%BD%E5%8A%A0) を参照する

3. 再起動して boot 設定を反映し、Klipper に printer.cfg を読ませる。

    ```sh
    sudo reboot
    ```

4. ブラウザで `http://<ホスト名>.local:8080` を開く。自機の塗布ページが開けば導入は終わり。

`config/` は git 管理外にある（実測値の書き換えでリポジトリが dirty にならないようにするため）。
テンプレートの規約と printer.cfg の扱いは [マシン設定テンプレート](../deploy/templates/README.md) にある。

## 開かないとき

| 症状                                        | 確認すること                                                                                      |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `<ホスト名>.local` に届かない               | 同じ LAN にいるか。IP アドレスで `http://<IP>:8080` を開く                                        |
| ページが 503 になる                         | `systemctl status pcbasm-api` で backend が動いているか。ログは `journalctl -u pcbasm-api -n 200` |
| 全ページに設定エラーが出る                  | `config/machine.toml` の誤り。理由が画面に出るので、設定ページか設定履歴から直す                  |
| `pcbnew` / `picamera2` の import が失敗する | `./install.sh packages` と `./install.sh python` をやり直す                                       |
| 画像の同梱資産が読めない                    | Git LFS の実体が無い。`./install.sh packages` をやり直すか `git lfs pull` を実行する              |
