# pnp

Pick and Place（部品実装）のドメインロジックを置く場所。machine.toml に `[pnp]` がある機体で使う。
塗布（`pasting/`）と同じ形でここへ足していく。

## いまあるもの

| モジュール       | 中身                                                                                                                                                                          |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `servo_setup.py` | ピンセットの 2 台のサーボの役割判別（Rotation / Open Close）と、Open Close の閉じ切り・開き切りの検証と角度制限（Rotation の制限はジョブが外す）。ジョブ `servo_setup` が使う |

## 置くもの

- 部品をピンセットでつかみ、運び、置く機械手順（吸着式ではない。設計は [ピンセット式 PnP の運動制御](../../../docs/design/pnp-tweezer-motion.md)）
- 実装の前計画（どの部品をどの順に、どのフィーダーから置くか）
- `[pnp]` の下位表の型（`pcbasm/config/pnp.py` の `PnpConfig` に足す）

ユーザー対話（prompt）・成果物の保存・ジョブの登録は `web/api/jobs/` が担う（塗布と同じ）。
PnP のジョブは、塗布のジョブと同じく機体に `pnp` 機能があるときだけ登録する。

## 使うもの

| 使う単位         | 使い方                                                                                 |
| ---------------- | -------------------------------------------------------------------------------------- |
| `pcbasm.align`   | 基準点と銅箔照合で、基板の置き方（board → 機械座標）と補正を測る。塗布と同じ入口を使う |
| `pcbasm.board`   | 部品の位置・向き・pad（`Board` / `Component`）                                         |
| `pcbasm.kicad`   | KiCad の基板の読み込み                                                                 |
| `pcbasm.machine` | 装置（Klipper・カメラ）と駐機                                                          |
| `pcbasm.hal`     | ピンセットのサーボ（`ServoBus`・`ServoMode`・`scan`・`wait_stopped`・`hold_position`） |
| `pcbasm.config`  | `MachineConfig.pnp`（`[pnp]`）と共通の節。開閉の範囲の規則（`open_close_span_error`）  |

## 依存方向

`pnp` と `pasting` は同じ層にあり、互いに import しない（`tests/test_architecture.py` が固定する）。
両方が使うものは `align` / `board` / `machine` などの下の層へ置く。
