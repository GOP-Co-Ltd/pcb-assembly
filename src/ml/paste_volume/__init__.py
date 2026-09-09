"""画像から塗布量を推定するモデルのドメイン層.

:mod:`pcbasm.pasting.dataset` が収集した dataset を、:mod:`ml` のコアへ繋ぐ。``session`` が
1 収集 session を検証し、``index`` が学習 sample の一覧を作り、``dataset`` が 1 sample を読み、
``batch`` が batch へ詰め、``task`` が :class:`ml.training.data.TrainingData` として Trainer へ
渡す。

装置ドメインへは収集 schema（:mod:`pcbasm.pasting.dataset.metadata`）と純粋な値オブジェクト
だけを参照する。制御ロジックは参照しない。学習側がこの schema を読み、装置側が export 済み
成果物を読むので package 単位では循環するが、1 つの関心事の両端なので意図した形。

この層と :mod:`pcbasm.pasting` は責務が逆向きになる。学習・評価・export はここ、装置の制御と
成果物の利用は :mod:`pcbasm.pasting`。
"""
