"""設定の層合成と、wheel 同梱設定の所在.

設定の既定値は attrs のクラスにのみ置き、TOML には差分だけを書く。

同じ既定値を 2 箇所で管理すると、どちらが効いているかを読み手が追えなくなる。

このパッケージは stdlib の ``tomllib`` しか使わないため、``ml-runtime`` すら
install していない環境でも import できる。

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import
する（例: ``from ml.config.composition import ConfigComposition``）。
"""
