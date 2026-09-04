"""学習・評価に共通するデータパイプライン基盤.

- :mod:`ml.data.image`: サイズ制約、幾何 augmentation、SampleLayerNorm
- :mod:`ml.data.batch`: pixel budget batching と stride 揃えの padding
- :mod:`ml.data.split`: group 単位の split と leave-one-group-out 計画
"""
