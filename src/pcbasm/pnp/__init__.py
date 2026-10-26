"""Pick and Place（部品実装）の制御.

machine.toml に ``[pnp]`` がある機体の、部品を吸着して基板へ置く手順を置く場所。
いまは中身が無く、PnP を実装するときにここへ足す（構成は README.md）。

塗布（:mod:`pcbasm.pasting`）と同じ層に置き、互いに import しない。
基板の位置合わせは :mod:`pcbasm.align`、基板の設計情報は :mod:`pcbasm.board` と :mod:`pcbasm.kicad`、装置は :mod:`pcbasm.machine` を使う。
"""
