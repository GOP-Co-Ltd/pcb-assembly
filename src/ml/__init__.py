"""機械学習の基盤とドメイン層.

PyTorch による実験・評価・最適化・export を担う。装置の制御は :mod:`pcbasm` の責務で、
機械学習の責務はこちらに置く。

コア（``artifact`` / ``config`` / ``data`` / ``evaluation`` / ``experiment`` / ``export`` /
``model`` / ``training`` / ``tuning``）はドメイン非依存で、:mod:`pcbasm` / :mod:`web` を
一切参照しない。塗布量推定のドメイン層 :mod:`ml.paste_volume` だけが例外で、収集 dataset を
読むために装置ドメインの**データ構造**を参照する。制御ロジックは参照しない。

この線引きと、学習コンテナに無い :mod:`pcbasm.hal` / pcbnew / picamera2 へ推移的にも
届かないことは ``tests/ml/test_architecture.py`` が機械検証する。

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import
する（例: ``from ml.artifact.package import ImmutablePackage``）。
"""
