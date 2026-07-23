# pasting

ペースト塗布専用の制御モジュール。

- ペースト塗布とフィルパス生成
- 基板共通の平均XY補正を塗布padと初回パージへ適用
- WebUIの基板overrideによる位置合わせ目標成功数の設定（未指定時はmachine既定を継承）
- 吐出量キャリブレーション・ローディング
- プローブによる基板表面の高さ計測

位置合わせは `posctrl` を利用し、基板overrideは `paste_solder` と `board_tour` で
共用する。
