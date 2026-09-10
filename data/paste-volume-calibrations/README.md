# 塗布量校正ファイル

`paste_volume_calibration`（塗布量校正の生成）と `paste_volume_refit`
（再フィット）が書き出す、直径ベース塗布量校正の保存先。
1 校正 = 1 session = 1 ファイル（`<slug>.paste-volume.json`）。

**この directory は Git 管理する。** 校正は装置設定と同じく再現に要る資産で、
どのペースト・ノズル径・塗布高さで測ったものかを履歴として残す必要がある
（収集 dataset を置く `data/paste-volume-datasets/` は容量の都合で ignore している）。

手法・schema・達成条件は
[直径ベースの塗布量校正](../../docs/paste-volume-diameter-calibration.md) を参照。
