# セットアップ

[ドキュメント一覧](../README.md)

## ハードウェア

- Raspberry Pi 5
- Pick and PlaceまたはPaste Dispenser Machine
- スピーカー（通知音を使う場合。I2S DAC / アンプまたは HDMI 音声出力）

## ソフトウェア

- RPi OS 64bit
- [Klipper](https://www.klipper3d.org/)
    - [KIAUH](https://github.com/dw-0/kiauh) 経由でインストール
        - Klipper + Moonraker + Mailsail
- [KiCAD](https://www.kicad.org/download/linux/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- `alsa-utils`（通知音を再生する `aplay` を含む）

OS以外のソフトウェア類は[`scripts/install-softwares.sh`](../scripts/install-softwares.sh)を実行

Raspberry Pi のカメラ・スピーカーと Klipper MCU firmware は、ソフトウェアの
インストール後に対話式スクリプトで設定する。

```sh
./scripts/setup-hardware.sh
```

- カメラは CAMERA port 0 / 1 と driver（`ov9281` または手入力）を選ぶ
- スピーカーは `max98357a`、overlay 名の手入力、未設定から選ぶ
- BTT SKR Pico v1.0 は画面の案内に従って BOOT jumper と RESET を操作し、
    `/media/$USER/RPI-RP2` volume へ同梱 UF2 firmware をコピーする
- 最後に案内される `sudo reboot` で boot 設定を反映する

boot 設定は `/boot/firmware/config.txt`（旧 OS では `/boot/config.txt`）へ反映され、
変更前の内容は同じ場所の `config.txt.pcbasm.bak` に保存される。スクリプトが管理する
marker 内だけを再実行時に置換し、それ以外の既存設定は保持する。

## GitHub Actions Runner

専用のRaspberry Pi 5をGitHub Actionsのself-hosted runnerとして構築する場合は、
[`github-runner/README.md`](../github-runner/README.md)を参照する。

## Python 環境

上記のソフトウェアをインストールしたうえで、次を実行

```sh
make setup
```

- VSCodeでリモートアクセスし、開発することを推奨する。

## マシン設定の配置

装置を動かす前に、テンプレートから `config/` を作る。

```sh
./scripts/setup-machine-config.sh  # data/config-templates/ から選んで config/ を作る
sudo systemctl restart klipper  # printer.cfg の反映
```

`config/` は git 管理外（実測値の書き換えでリポジトリが dirty にならないようにするため）。
テンプレートの規約と printer.cfg の運用は
[`data/config-templates/README.md`](../data/config-templates/README.md) を参照。

開発に参加する場合は [CONTRIBUTING](../CONTRIBUTING.md) を参照する。

セットアップ後は [WebUI の使い方](webui.md) へ進む。常駐起動は [運用ガイド](operations.md) を参照する。
