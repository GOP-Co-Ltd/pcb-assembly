"""点塗布の円直径から塗布量を推定する校正.

塗布後画像に写るはんだの円は、直径と体積が 3 次の関係にある。
「直径 → 体積」の 3 次近似を作り、運転時の ``rotations_per_ul`` 補正へ使う。

- :mod:`pcbasm.pasting.paste_volume.detect` — 検出ハイパラと 1 view の直径計測
- :mod:`pcbasm.pasting.paste_volume.aggregate` — 複数 view の直径を中央値へ畳む
- :mod:`pcbasm.pasting.paste_volume.model` — 切片 0 固定の 3 次モデルとフィット
- :mod:`pcbasm.pasting.paste_volume.calibration` — 校正ファイルの schema と読み書き
- :mod:`pcbasm.pasting.paste_volume.estimator` — 校正から塗布量を推定する公開 API

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import する
（``import pcbasm.pasting`` が cv2 を引き込まない契約を ``tests/test_package.py`` が固定する）。
"""
