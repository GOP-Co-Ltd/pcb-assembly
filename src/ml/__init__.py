"""ドメイン非依存の機械学習基盤.

PyTorch による実験・評価・最適化・export の共通部分を提供する。装置制御ドメイン
(:mod:`pcbasm` / :mod:`web`) を一切参照せず、依存の向きは常にドメイン側から
``ml`` への一方向とする。

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import
する（例: ``from ml.artifact.package import publish_immutable_package``）。
"""
