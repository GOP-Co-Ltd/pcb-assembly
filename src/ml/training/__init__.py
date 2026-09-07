"""学習 loop と、その周辺の契約.

- :mod:`ml.training.task`: 学習 task の ABC と Gaussian 回帰の具象 task
- :mod:`ml.training.data`: epoch 計画と batch materialize の ABC
- :mod:`ml.training.transaction`: gradient accumulation group の commit 契約
- :mod:`ml.training.checkpoint`: 進捗・best 選択・checkpoint の永続化
- :mod:`ml.training.random_state`: 乱数状態の seed・捕捉・復元
- :mod:`ml.training.loop`: epoch ループ、early stopping、deadline、signal
"""
