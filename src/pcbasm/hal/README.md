# HAL

Hardware Abstraction Layer: カメラ・Klipper（Moonraker 経由）・音声出力を扱うクラスを置く。
画像処理は `pcbasm.vision`、塗布や位置合わせの手順は `pcbasm.pasting` / `pcbasm.posctrl` に置く。

## 構成

| モジュール           | 役割                                                                                                                                  |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `klipper.py`         | Moonraker REST API のラッパー `Klipper`（G-code 送信・状態取得）と読み取り専用の `ReadonlyKlipper`                                    |
| `stage.py`           | XYZ ステージ `XYZStage`（現在位置・可動域の取得、移動 G-code の生成と可動域検証）、`Speed` / `Limits`                                 |
| `manual_stepper.py`  | Klipper `manual_stepper` の G-code 生成 `ManualStepper`                                                                               |
| `air_pump.py`        | エアポンプの ON/OFF G-code 生成 `AirPump`                                                                                             |
| `paste_dispenser.py` | オーガースクリュー式ディスペンサー `PasteDispenser`。μL を `rotations_per_ul` で回転数に換算し、`ManualStepper` と `AirPump` を束ねる |
| `camera.py`          | カメラの抽象基底 `Camera` と生成関数 `create_camera`（backend `"usb"` = V4L2、`"csi"` = picamera2）                                   |
| `framehub.py`        | 専有スレッド 1 本で capture し、最新フレームを複数の消費者へ配る `FrameHub`                                                           |
| `audio.py`           | ALSA の通知音再生 `AlsaAudioPlayer`。音源は `sounds/` の WAV                                                                          |

公開名は `pcbasm.hal` から re-export している（`from pcbasm.hal import Klipper, XYZStage`）。

## 機械を動かす流れ

1. `XYZStage` / `ManualStepper` / `AirPump` / `PasteDispenser` は `ReadonlyKlipper` を受け取る
2. これらのメソッドは機械を動かさず、`pcbasm.gcode.GCode` を返す
3. 呼び出し側が返った `GCode` を `Klipper.send_gcode()` で送って初めて機械が動く

`ReadonlyKlipper` は `Klipper.readonly` で得る。状態取得（`get_status` / `get_config` / `get_macros` / `has_macro`）だけを持ち、G-code を送れない。

## 変更時の注意

- 新しいデバイスを足すときも、上の流れに合わせて「`ReadonlyKlipper` で設定を読み、`GCode` を返す」形にする
- 機械設定（TOML）から HAL を組み立てるのは呼び出し側（`web/api/`、`posctrl.setup`、`pasting.applicator.build_applicator`）。HAL は `pcbasm.config` を読まない（例外は `audio.py`）
- テストの fake は `tests/helpers.py` の `FakeCamera` / `FakeKlipper` / `FakeAudioPlayer`。`make api-fake` の固定画像カメラは `web/api/fake_camera.py` にある
- 実機に触るテストは `@mark_hardware` を付ける（skill `hardware-test`）
