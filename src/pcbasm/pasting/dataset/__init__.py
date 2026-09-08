"""ペースト塗布画像 dataset の収集・永続化.

- :mod:`pcbasm.pasting.dataset.plan` — 銅板のセル格子・吐出量スイープ・view の計画と事前検証
- :mod:`pcbasm.pasting.dataset.metadata` — metadata.json の DTO と strict な parse（schema v2）
- :mod:`pcbasm.pasting.dataset.writer` — 一時 session への PNG 書き込みと atomic 確定
- :mod:`pcbasm.pasting.dataset.recorder` — 撮影・塗布結果の蓄積と metadata 組立
- :mod:`pcbasm.pasting.dataset.capture` — セル中心での撮影と固定寸法 crop

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import する。
"""
