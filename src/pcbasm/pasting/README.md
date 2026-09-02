# pasting

ペースト塗布専用の制御モジュール。

- ペースト塗布とフィルパス生成
- 吐出量キャリブレーション・ローディング
- ペースト体積datasetの実機収集とmetadata永続化
- プローブによる基板表面の高さ計測

位置合わせは `posctrl` を利用する。
画像からの体積推論は必要な処理内で`ml.paste_volume.infer`を読み込み、学習・評価・exportの
実装はtop-levelの`ml` packageへ置く。収集schemaは`paste_dataset.py`が所有し、
`ml.paste_volume.data`から再利用する。
