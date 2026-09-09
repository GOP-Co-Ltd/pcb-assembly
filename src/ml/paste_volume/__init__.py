"""画像から塗布量を推定するモデルのドメイン層.

:mod:`pcbasm.pasting.dataset` が収集した dataset を、ドメイン非依存の ML 基盤 :mod:`ml` へ
繋ぐ。``session`` が 1 収集 session を検証し、``index`` が学習 sample の一覧を作り、
``dataset`` が 1 sample を読み、``batch`` が batch へ詰め、``task`` が
:class:`ml.training.data.TrainingData` として Trainer へ渡す。

torch を import してよいが、``pcbasm.pasting.__init__`` からは re-export しない
（``import pcbasm.pasting`` が torch を引き込まない契約を ``tests/test_package.py`` が固定）。
"""
