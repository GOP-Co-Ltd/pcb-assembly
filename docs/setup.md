# セットアップ

[ドキュメント一覧](../README.md)

## ハードウェア

- Raspberry Pi 5
- Paste Dispenser Machine
- スピーカー（通知音を使う場合。I2S DAC / アンプまたは HDMI 音声出力）

## ソフトウェア

- RPi OS 64bit
- [Klipper](https://www.klipper3d.org/)
    - [KIAUH](https://github.com/dw-0/kiauh) 経由でインストール
        - Klipper + Moonraker + Mainsail
- [KiCAD](https://www.kicad.org/download/linux/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- `alsa-utils`（通知音を再生する `aplay` を含む）

OS 以外は `install.sh` が入れる。

## 手順

OS（Debian 13 trixie の Raspberry Pi OS 64bit）を入れ、ホスト名を機体ごとに一意な名前へ変えた Raspberry Pi 上で、次の順に実行する。

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
    - 全段実行の `services` は backend と frontend の両方を置く（同居機）。frontend を LAN の別ホストにまとめる機体では、
        導入後に `sudo systemctl disable --now pcbasm-ui` で frontend を止める

3. 再起動して boot 設定を反映し、Klipper に printer.cfg を読ませる。

    ```sh
    sudo reboot
    ```

frontend だけを置くホスト（機体でない Raspberry Pi など）は `./install.sh --ui-only` を使う（preflight / packages / python / services の ui だけ）。

`config/` は git 管理外（実測値の書き換えでリポジトリが dirty にならないようにするため）。
テンプレートの規約と printer.cfg の運用は
[`deploy/templates/README.md`](../deploy/templates/README.md) を参照。

開発は VSCode でリモートアクセスして行うことを推奨する。
開発に参加する場合は [CONTRIBUTING](../CONTRIBUTING.md) を参照する。

セットアップ後は [立ち上げ](commissioning.md) の順に機体を整え、[WebUI の使い方](webui.md) へ進む。常駐起動は [運用ガイド](operations.md) を参照する。

## GitHub Actions Runner

専用の Raspberry Pi 5 を GitHub Actions の self-hosted runner として構築する場合は、
[`github-runner/README.md`](../github-runner/README.md) を参照する。
