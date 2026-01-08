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

### machine.toml

Klipper以外のハードウェア設定。GPIO、カメラ、その他のデバイス設定を記述する。

```toml
[klipper]
host = "localhost"
port = 7125

[probe]
a_pin = 17
b_pin = 27
rotation_pulse = 600      # PPR (Pulses Per Rotation)
rotation_distance = 40.0  # mm/回転
inverse = false

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

- マシン名は小文字のスネークケース（例: `pd_china_frame`）
- 略称を使う場合は先頭に付ける（例: `pd_` = paste dispenser）
