# posctrl

Board/オフセットの位置合わせを担う共通制御モジュール。

- マシン初期化と Board 変換計測のセットアップ
- Board / 位置 / オフセットの調整
- 設計銅箔のカメラ投影と観測エッジ照合（chamfer）による位置ずれ算出
- 部品単位の銅箔照合による自動位置合わせ（PadAligner、部品の pad 群の実銅箔を覆う ROI で照合）
- 位置合わせの配線と補正結果の lookup（PadAlignmentSession / ComponentAlignments / sorted_top_component_pads）
- Board 巡回用のカメラ表示
- カメラ校正用のチェッカーボード多視点スキャン（`checkerboard_scan.py` の `CheckerboardScanner`）

観測は `observe() -> Transform`（カメラ mm 空間、原点=画像中心、想定→観測）契約の observer で統一している。

チェッカーボードスキャンは、ボードを固定したまま XY ステージを動かして `cv2.calibrateCamera` 用の
多視点を稼ぐ。訪問順は **中心 → 隅 → 蛇行**（中心が残差の基準視点になるので先頭に置く）。移動は
開始位置を基準とした絶対座標で行い誤差を累積させず、巡回中は **Z を一切動かさない**
（操作者が合わせたフォーカス Z を壊さないため）。終了時は `finally` で必ず開始位置へ戻る。

ペースト塗布と Pick and Place の双方から利用する。
