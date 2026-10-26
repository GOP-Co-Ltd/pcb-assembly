# 設定キー一覧

[ドキュメント一覧](../../README.md) · [ファイルと環境変数](files.md)

`config/machine.toml` に書けるキーのうち、WebUI の設定ページ（機体タブの「マシン設定」）に並ぶ項目の一覧。
節は設定ページの 1 画面、カードは画面の中の見出しにあたる。
既定値のあるキーは書かなければ既定値で動き、設定ページでは空欄に既定値が薄く出る。
既定値の無いキーを書き忘れると設定エラーになる。

`[solder]` ははんだ塗布機だけが持つ節で、部品実装だけの機体（`[pnp]` だけ）の設定ページには出ない。
`[pnp]` はまだ項目を持たない。

設定ページに出ない節が 1 つある。

- `[klipper]`: Moonraker の `host` と `port`。既定値は無く、必ず書く（テンプレートは `localhost` と `7125`）

値の範囲は書込時と起動時に確かめ、範囲外なら設定エラーになる（[保守ガイド](../maintenance.md) の設定エラーからの復旧）。

このファイルは設定のスキーマから生成する。手で直さず、`uv run python -m tests.support.config_reference > docs/reference/config.md` の後に `make format` を実行する。

## はんだ塗布（`[solder]`）

| カード                 | キー                                                                        | 表示名                          | 単位   | 値                                                                                       |
| ---------------------- | --------------------------------------------------------------------------- | ------------------------------- | ------ | ---------------------------------------------------------------------------------------- |
| 吐出の基本             | `solder.dispenser.rotations_per_ul`                                         | 1uLあたりの回転数               | rev/uL | 数値                                                                                     |
| 吐出の基本             | `solder.nozzle.diameter`                                                    | ノズル内径                      | mm     | 数値                                                                                     |
| 吐出の基本             | `solder.nozzle.outer_diameter`                                              | ノズル外径                      | mm     | 数値                                                                                     |
| 吐出の基本             | `solder.paste_density`                                                      | はんだペースト密度              | mg/uL  | 数値                                                                                     |
| 吐出の基本             | `solder.defaults.ul_per_mm2`                                                | 面積あたりのペースト量          | uL/mm² | 数値                                                                                     |
| 吐出の速度             | `solder.dispenser.max_dispense_rate`                                        | 吐出レート上限                  | uL/s   | 数値                                                                                     |
| 吐出の速度             | `solder.dispenser.dispense_accel`                                           | 吐出加速度                      | uL/s²  | 数値                                                                                     |
| 吐出の速度             | `solder.motion.max_fill_speed`                                              | 最大塗布速度                    | mm/s   | 数値                                                                                     |
| リトラクト             | `solder.dispenser.retract_amount`                                           | リトラクション量                | uL     | 数値                                                                                     |
| リトラクト             | `solder.dispenser.retract_rate`                                             | リトラクションレート            | uL/s   | 数値                                                                                     |
| リトラクト             | `solder.dispenser.retract_accel_factor`                                     | リトラクション加速度係数        |        | 数値                                                                                     |
| プライム・パージ       | `solder.defaults.prime_extra_delay`                                         | プライム後追加遅延              | s      | 数値（0 以上）                                                                           |
| プライム・パージ       | `solder.initial_purge_ul`                                                   | 初回パージ量                    | uL     | 数値（0 以上）                                                                           |
| 塗布方式               | `solder.defaults.dispense_mode`                                             | 塗布方式                        |        | `auto`（Auto） / `dot`（点） / `line`（線） / `area`（面） / `micro_line`（微小線）      |
| 塗布方式               | `solder.defaults.line_direction`                                            | 線の走行方向                    |        | `unconstrained`（指定なし） / `outward`（部品中心 → 外側） / `inward`（外側 → 部品中心） |
| Auto の切り替え        | `solder.auto.line_aspect_ratio`                                             | Auto線塗布しきい縦横比          |        | 数値                                                                                     |
| Auto の切り替え        | `solder.auto.area_short_side_factor`                                        | Auto面塗布しきい短辺倍率        |        | 数値                                                                                     |
| 塗布経路               | `solder.defaults.bead_width_factor`                                         | ビード幅係数                    |        | 数値                                                                                     |
| 塗布経路               | `solder.defaults.overlap`                                                   | 重なり率                        |        | 数値（0 以上）                                                                           |
| 塗布経路               | `solder.defaults.boundary_margin`                                           | 外周余白                        | mm     | 数値                                                                                     |
| 微小線                 | `solder.defaults.micro_line_z_start`                                        | 始点高                          | mm     | 数値か `auto`                                                                            |
| 微小線                 | `solder.defaults.micro_line_z_end`                                          | 終点高                          | mm     | 数値か `auto`                                                                            |
| 微小線                 | `solder.motion.micro_line_settle_seconds`                                   | 微小線の静定待ち                | s      | 数値（0 以上）                                                                           |
| Z 高さ                 | `solder.defaults.paste_height`                                              | 塗布高さ                        | mm     | 数値か `auto`                                                                            |
| Z 高さ                 | `solder.motion.lift_height`                                                 | 吐出後の上昇高さ                | mm     | 数値                                                                                     |
| Z 高さ                 | `solder.motion.post_dispense_settle_seconds`                                | 塗布後の静定待ち（上昇前）      | s      | 数値（0 以上）                                                                           |
| ツールヘッド           | `solder.nozzle.offset.x`                                                    | ツールヘッド相対位置 X          | mm     | 数値                                                                                     |
| ツールヘッド           | `solder.nozzle.offset.y`                                                    | ツールヘッド相対位置 Y          | mm     | 数値                                                                                     |
| 流量キャリブレーション | `solder.volume_correction.calibration_file`                                 | 校正ファイル                    |        | 文字列。空欄で無効                                                                       |
| 流量キャリブレーション | `solder.volume_correction.amount_ul`                                        | 1点あたりの塗布量               | uL     | 数値                                                                                     |
| 流量キャリブレーション | `solder.volume_correction.crop_size_mm`                                     | 撮影crop寸法                    | mm     | 数値                                                                                     |
| 流量キャリブレーション | `solder.volume_correction.settle_seconds`                                   | 静定待ち                        | s      | 数値（0 以上）                                                                           |
| ノズル位置             | `solder.nozzle.cap.x` / `solder.nozzle.cap.y` / `solder.nozzle.cap.z`       | キャップ位置                    | mm     | 座標。3 つそろえるか、すべて書かない                                                     |
| ノズル位置             | `solder.nozzle.clean.x` / `solder.nozzle.clean.y` / `solder.nozzle.clean.z` | クリーニング位置（Zは面の高さ） | mm     | 座標。3 つそろえるか、すべて書かない                                                     |
| ノズルクリーニング     | `solder.nozzle.clean.press_depth`                                           | 押し込み量                      | mm     | 数値（0 以上）                                                                           |
| ノズルクリーニング     | `solder.nozzle.clean.purge_ul`                                              | クリーニング前パージ量          | uL     | 数値（0 以上）                                                                           |
| ノズルクリーニング     | `solder.nozzle.clean.stroke`                                                | こすり幅 片側                   | mm     | 数値（0 以上）                                                                           |
| ノズルクリーニング     | `solder.nozzle.clean.passes`                                                | 往復回数                        |        | 整数（0 以上）                                                                           |
| ノズルクリーニング     | `solder.nozzle.clean.wipe_speed`                                            | こすり速度                      | mm/sec | 数値                                                                                     |
| 所要時間の予測         | `solder.tact.setup_sec`                                                     | 前段の時間                      | s      | 数値（0 以上）                                                                           |

## 位置合わせ（`[alignment]`）

| カード                   | キー                              | 表示名                       | 単位 | 値             |
| ------------------------ | --------------------------------- | ---------------------------- | ---- | -------------- |
| 基準円・塗布痕の位置補正 | `alignment.tolerance`             | 位置合わせ許容誤差           | mm   | 数値           |
| 銅箔照合                 | `alignment.region_size_px`        | 照合領域の一辺               | px   | 整数（1 以上） |
| 銅箔照合                 | `alignment.region_overlap`        | 照合領域の重なり             |      | 数値（0 以上） |
| 銅箔照合                 | `alignment.board_edge_margin`     | 基板外形からの余白           | mm   | 数値           |
| 銅箔照合                 | `alignment.search_window`         | 探索窓 片側幅                | mm   | 数値           |
| 銅箔照合                 | `alignment.max_correction`        | 最大補正量                   | mm   | 数値           |
| 銅箔照合                 | `alignment.max_passes`            | 再計測の上限回数             |      | 整数（1 以上） |
| 銅箔照合                 | `alignment.converge_tolerance`    | 収束判定の増分               | mm   | 数値           |
| 銅箔照合                 | `alignment.canny_low`             | Canny下側閾値                |      | 数値           |
| 銅箔照合                 | `alignment.canny_high`            | Canny上側閾値                |      | 数値           |
| 銅箔照合                 | `alignment.blur_ksize`            | ブラーカーネルサイズ         |      | 整数（1 以上） |
| pad ごとの逐次位置合わせ | `alignment.refine_max_short_side` | 逐次位置合わせ対象の最大短辺 | mm   | 数値（0 以上） |

## プローブ（`[probe]`）

| カード   | キー                      | 表示名                 | 単位 | 値             |
| -------- | ------------------------- | ---------------------- | ---- | -------------- |
| プローブ | `probe.lift_height`       | プローブ後の上昇高さ   | mm   | 数値           |
| プローブ | `probe.min_radius`        | 銅箔境界からの最小距離 | mm   | 数値           |
| プローブ | `probe.board_edge_margin` | 基板外形からの最小距離 | mm   | 数値           |
| プローブ | `probe.min_samples`       | 最小サンプル数         |      | 整数（6 以上） |
| プローブ | `probe.max_samples`       | 最大サンプル数         |      | 整数           |

## 基準点（`[reference_point]`）

| カード             | キー                                    | 表示名                       | 単位 | 値       |
| ------------------ | --------------------------------------- | ---------------------------- | ---- | -------- |
| 位置               | `reference_point.x`                     | 基準点 X                     | mm   | 数値     |
| 位置               | `reference_point.y`                     | 基準点 Y                     | mm   | 数値     |
| 位置               | `reference_point.target_diameter`       | 基準点マーカー直径           | mm   | 数値     |
| 位置               | `reference_point.diameter_tolerance_mm` | 基準点マーカー直径の許容誤差 | mm   | 数値     |
| コーナーオフセット | `reference_point.offsets.top_left`      | 左上 [x, y]                  | mm   | `[x, y]` |
| コーナーオフセット | `reference_point.offsets.top_right`     | 右上 [x, y]                  | mm   | `[x, y]` |
| コーナーオフセット | `reference_point.offsets.bottom_left`   | 左下 [x, y]                  | mm   | `[x, y]` |
| コーナーオフセット | `reference_point.offsets.bottom_right`  | 右下 [x, y]                  | mm   | `[x, y]` |

## カメラ（`[camera]`）

| カード   | キー                      | 表示名                     | 単位 | 値             |
| -------- | ------------------------- | -------------------------- | ---- | -------------- |
| デバイス | `camera.calibration_file` | キャリブレーションファイル |      | 文字列         |
| デバイス | `camera.device_id`        | デバイスID                 |      | 整数           |
| デバイス | `camera.width`            | 幅                         | px   | 整数           |
| デバイス | `camera.height`           | 高さ                       | px   | 整数           |
| デバイス | `camera.fps`              | フレームレート             | fps  | 数値           |
| デバイス | `camera.format`           | ピクセルフォーマット       |      | 文字列         |
| クロップ | `camera.crop.width`       | クロップ幅                 | px   | 整数（1 以上） |
| クロップ | `camera.crop.height`      | クロップ高さ               | px   | 整数（1 以上） |

## 静定待ち（`[settle]`）

| カード   | キー               | 表示名           | 単位 | 値             |
| -------- | ------------------ | ---------------- | ---- | -------------- |
| 静定待ち | `settle.move_sec`  | 移動後の静定待ち | s    | 数値（0 以上） |
| 静定待ち | `settle.probe_sec` | プローブ後の待ち | s    | 数値（0 以上） |

## 統計検出（`[detection]`）

| カード   | キー                             | 表示名              | 単位 | 値             |
| -------- | -------------------------------- | ------------------- | ---- | -------------- |
| 統計検出 | `detection.sample_count`         | 1観測のフレーム数   |      | 整数（1 以上） |
| 統計検出 | `detection.minimum_sample_count` | 1観測に必要な検出数 |      | 整数（1 以上） |

## 通知音（`[audio]`）

| カード | キー           | 表示名       | 単位 | 値             |
| ------ | -------------- | ------------ | ---- | -------------- |
| 通知音 | `audio.device` | 出力デバイス |      | 文字列         |
| 通知音 | `audio.volume` | 音量         |      | 数値（0 以上） |
