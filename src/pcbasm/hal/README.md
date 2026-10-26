# HAL

Hardware Abstraction Layer: カメラ・Klipper（Moonraker 経由）・音声出力を扱うクラスを置く。
画像処理は `pcbasm.vision`、塗布や位置合わせの手順は `pcbasm.pasting` / `pcbasm.align` に置く。

## 構成

| モジュール           | 役割                                                                                                                                                                               |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `klipper.py`         | Klipper の差し替え口 `KlipperClient`（Protocol）と Moonraker 実装 `MoonrakerKlipper`、生成口 `open_klipper`、Klipper が断ったときの `KlipperError`（`RuntimeError` 派生）          |
| `stage.py`           | XYZ ステージ `XYZStage`（現在位置・可動域の取得、移動 G-code の生成と可動域検証。静定待ち付きの `move_settled`、Z を上げて近づく `travel_to`）、`Speed` / `Limits`                 |
| `manual_stepper.py`  | Klipper `manual_stepper` の G-code 生成 `ManualStepper`                                                                                                                            |
| `air_pump.py`        | エアポンプの ON/OFF G-code 生成 `AirPump`                                                                                                                                          |
| `paste_dispenser.py` | オーガースクリュー式ディスペンサー `PasteDispenser`。μL を `rotations_per_ul` で回転数に換算し、`ManualStepper` と `AirPump` を束ねる                                              |
| `camera.py`          | カメラの抽象基底 `Camera` と生成関数 `create_camera`（backend `"usb"` = V4L2、`"csi"` = picamera2）                                                                                |
| `framehub.py`        | 専有スレッド 1 本で capture し、最新フレームを複数の消費者へ配る `FrameHub`                                                                                                        |
| `audio.py`           | ALSA の通知音再生 `AlsaAudioPlayer`。音源は `sounds/` の WAV                                                                                                                       |
| `fake.py`            | 実機の代わりの `FakeKlipper` / `FakeCamera` / `SceneCamera` / `FakeAudioPlayer`。テストと `make dev` が共用する。`SceneCamera` は `FakeKlipper` の位置から見える図形と塗布痕を描く |

公開名は `pcbasm.hal` から re-export している（`from pcbasm.hal import KlipperClient, XYZStage`）。

## 機械を動かす流れ

1. `XYZStage` / `ManualStepper` / `AirPump` / `PasteDispenser` は `KlipperClient` を受け取り、状態と printer.cfg を読む
2. これらのメソッドは機械を動かさず、`pcbasm.gcode.GCode` を返す
3. 呼び出し側が返った `GCode` を `KlipperClient.send_gcode()` で送って初めて機械が動く
4. ジャーナル（`pcbasm.machine.journal.Journal`）を渡した接続では、`send_gcode()` は送る前にブロックをそこへ書く
5. `MoonrakerKlipper.connection()` / `FakeKlipper.connection()` は 1 つのクライアントを共有したまま、既定の timeout とジャーナルだけを替えた接続を返す。`MoonrakerKlipper` は `printer.cfg` を接続ごとに最初の 1 回だけ読む

`KlipperClient` の実装は `MoonrakerKlipper` と `fake.FakeKlipper` の 2 つに限る。

## 変更時の注意

- 新しいデバイスを足すときも、上の流れに合わせて「`KlipperClient` で設定を読み、`GCode` を返す」形にする
- 機械設定（TOML）から Klipper とカメラを組み立てるのは `pcbasm.machine.devices` だけ。`open_klipper` を呼ぶのもそこだけにする。HAL は `pcbasm.config` を読まない（`open_klipper` の接続先、通知音の出力先と音量は値で受け取る）
- fake は `fake.py` に 1 系統だけ置く。テスト用に別の fake を作らず、足りない模擬はここへ足す
- テストは実機に触れない。実機での確認は [実機受け入れ試験](../../../docs/acceptance.md) に書く
