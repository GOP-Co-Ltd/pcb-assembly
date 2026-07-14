# configs ディレクトリ規約

このディレクトリにはマシンごとの設定ファイルを格納する。

## ディレクトリ構造

```
configs/
├── README.md
├── <machine_name>/
│   ├── printer.cfg      # Klipper設定
│   └── machine.toml     # Klipper以外のハードウェア設定
└── <another_machine>/
    ├── printer.cfg
    └── machine.toml
```

## ファイル説明

### printer.cfg

Klipperの設定ファイル。MCU、ステッパー、ドライバ、マクロなどを記述する。

インストール方法:

```bash
./install-printer-cfg.sh
```

klipper.env の config パスを repo 実パスに向け（`SAVE_CONFIG` の較正値が git diff に現れる）、`~/printer_data/config/printer.cfg` に閲覧用シンボリックリンクを作成する。反映には Klipper の再起動が必要。

### machine.toml

Klipper以外のハードウェア設定。GPIO、カメラ、その他のデバイス設定を記述する。

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

## 命名規則

- マシン名は小文字のスネークケース（例: `kurousagi`）
- 略称を使う場合は先頭に付ける（例: `pd_` = paste dispenser）

`test-fixture/` はテスト・WebUI E2E 用のフィクスチャマシン（実機設定を汚さないための git 管理ダミー。書き込み検証後は `git checkout` で復元する）。
