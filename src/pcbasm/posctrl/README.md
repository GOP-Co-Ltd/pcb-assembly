# posctrl

Board/オフセットの位置合わせを担う共通制御モジュール。

- マシン初期化と Board 変換計測のセットアップ
- Board / 位置 / オフセットの調整
- 設計銅箔のカメラ投影と観測エッジ照合（chamfer、並進のみ）による位置ずれ算出
- 重複領域ベースの銅箔照合と pad ごとの局所補正
- Board 巡回用のカメラ表示

銅箔位置合わせでは、基板 outline より overlap 幅ぶん外側から、既定
100 px・overlap 0.5 の `AlignmentRegion` を計画する。対象 pad の所属は中心点で判定し、
中心点を含まない候補領域は計画から除外する。ROI が基板外へ出ることは許可し、outline を
既定 0.5 mm inset した pixel mask の外側を照合から除外する。

`RegionAligner` は各領域を反復計測し、既定 0.03 mm 以下の増分で収束した
`RegionAlignment` だけを採用する。照合失敗・補正上限超過・非収束の領域は
`RegionAlignmentSession` が棄却する。`BoardAlignment` は pad 中心を覆う全成功領域の
変位を平均して補正し、塗布前の確認で成功領域に覆われない pad が1つでもあれば
ジョブ全体を中止する。

Board / オフセット調整用の観測は `observe() -> Transform`
（カメラ mm 空間、原点=画像中心、想定→観測）契約の observer で統一している。

ペースト塗布と Pick and Place の双方から利用する。
