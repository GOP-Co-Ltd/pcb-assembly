"""回帰 model を組み立てるための部品と、その診断.

- :mod:`ml.model.blocks`: GroupNorm 畳み込み、residual stage、画像 encoder
- :mod:`ml.model.heads`: Gaussian 回帰 head と、encoder との合成 model
- :mod:`ml.model.multiview`: 共有 encoder と view 平均の多視点 model
- :mod:`ml.model.loss`: 重み付き Gaussian negative log likelihood
- :mod:`ml.model.inspection`: parameter 数と multiply-accumulate 数の実測
"""
