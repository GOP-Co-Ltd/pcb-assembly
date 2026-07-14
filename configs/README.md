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

[reference_point]
corner = "top_left"  # アンカーコーナー: top_left / top_right / bottom_left / bottom_right（省略時 top_left）
x = 14.0             # アンカーコーナーのマーカーのおおよその機械座標 (mm)
y = 1.5
target_diameter = 3.0 # 基準点マーカーの直径 (mm)
offset = [5.0, -5.0] # 基板コーナー → マーカー のオフセット [x, y] (mm)

[board_align]
# 基板コーナーの輪郭照合による board 変換計測
# 全キーに既定値あり・節省略可（ただし WebUI 設定エディタで編集するには行が必要）
tolerance = 0.05     # 各コーナーサーボの収束許容誤差 [mm]
max_correction = 2.0 # 照合ずれの上限 [mm]。超過は誤マッチとして棄却
search_window = 1.5  # 照合の探索窓 片側幅 [mm]
edge_length = 2.0    # コーナーROIの片側辺長 = 含める外形エッジ長 [mm]
theta_range = 2.0    # 回転探索の片側範囲 [deg]
canny_low = 100.0    # Cannyエッジ検出の下側閾値
canny_high = 200.0   # Cannyエッジ検出の上側閾値
blur_ksize = 5       # GaussianBlurカーネルサイズ (奇数)
```

基準点マーカーが必要なのはアンカーの1コーナーのみ。マーカーはカメラ回転計測と粗並進の初期推定にだけ使い、board 変換（基板座標→機械座標）は基板自身の4隅の外形輪郭照合で計測する（4隅すべての照合が必須、並進含む6DOFの最小二乗フィット）。基板外形 bbox の4隅 ±`edge_length` に外形ジオメトリが必要で、`edge_length + search_window` はカメラ視野短辺の半分以下にする。

## 命名規則

- マシン名は小文字のスネークケース（例: `pd_china_frame`）
- 略称を使う場合は先頭に付ける（例: `pd_` = paste dispenser）

`test-fixture/` はテスト・WebUI E2E 用のフィクスチャマシン（実機設定を汚さないための git 管理ダミー。書き込み検証後は `git checkout` で復元する）。
