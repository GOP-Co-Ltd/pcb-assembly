"""ペースト塗布画像 dataset の収集・永続化.

- :mod:`pcbasm.pasting.dataset.plan` — 銅板のセル格子・吐出量スイープ・view の計画と事前検証
- :mod:`pcbasm.pasting.dataset.metadata` — metadata.json の DTO と strict な parse（schema v3）
- :mod:`pcbasm.pasting.dataset.pending` — 計量質量だけが未確定の doc（pending.json、schema v1）
- :mod:`pcbasm.pasting.dataset.writer` — 一時 session への PNG 書き込みと atomic 確定、
    未確定 session の救出
- :mod:`pcbasm.pasting.dataset.recorder` — 撮影・塗布結果の蓄積と metadata 組立
- :mod:`pcbasm.pasting.dataset.reader` — 完成 session の列挙と読み出し

撮影と固定寸法 crop は dataset 外の :mod:`pcbasm.pasting.capture` を使う。

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import する。
"""
