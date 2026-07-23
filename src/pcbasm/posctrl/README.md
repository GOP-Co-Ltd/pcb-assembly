# posctrl

Board/オフセットの位置合わせを担う共通制御モジュール。

- マシン初期化と Board 変換計測のセットアップ
- Board / 位置 / オフセットの調整
- 設計銅箔のカメラ投影と観測エッジ照合（chamfer）による位置ずれ算出
- 他padの銅箔重心から `2 * max_correction + tolerance` より離れたTOP padを安全候補として選定
- 安全候補のうちComponentごとの最小面積padを優先し、残りを面積順の後続候補に使用
- 複数padの照合結果から回転を含まないXY平均補正を算出（PadAlignmentSession / PadAlignments）
- 既存の部品単位位置合わせAPI（ComponentAlignments / sorted_top_component_pads）
- Board 巡回用のカメラ表示

観測は `observe() -> Transform`（カメラ mm 空間、原点=画像中心、想定→観測）契約の observer で統一している。

ペースト塗布と Pick and Place の双方から利用する。

`paste_solder` と `board_tour` は同じ候補選定と補充処理を使う。machine既定は
照合成功10件、失敗3件まで許容する。安全候補が1件以上かつ目標未満なら
実行時の目標成功数を候補数まで下げ、候補がない場合や実行中の失敗で候補を
使い切った場合はジョブを中止する。
