# posctrl

Board/オフセットの位置合わせを担う共通制御モジュール。

- マシン初期化と Board 変換計測のセットアップ
- Board / 位置 / オフセットの調整
- 設計銅箔のカメラ投影と観測エッジ照合（chamfer）による位置ずれ算出
- 領域(ROI)単位の銅箔照合による自動位置合わせ（PadAligner、crop 由来サイズで分割した領域内 pad 群の実銅箔を覆う ROI で照合）
- 位置合わせの配線と補正結果の lookup（PadAlignmentSession / RegionAlignments / sorted_top_pad_regions）
- Board 巡回用のカメラ表示

観測は `observe() -> Transform`（カメラ mm 空間、原点=画像中心、想定→観測）契約の observer で統一している。

ペースト塗布と Pick and Place の双方から利用する。
