# pasting

ペースト塗布専用の制御モジュール。

- ペースト塗布とフィルパス生成
- 基板共通の平均XY補正を塗布padと初回パージへ適用
- WebUIの設定ページから `machine.toml` の位置合わせ目標成功数を設定
- 吐出量キャリブレーション・ローディング
- プローブによる基板表面の高さ計測

位置合わせは `posctrl` を利用する。`paste_dispenser.pad_align.sample_count` は
machine単位の設定で、`paste_solder` と `board_tour` が共用する。
