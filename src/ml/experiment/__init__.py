"""実験記録の抽象と、再現性メタデータの収集.

- :mod:`ml.experiment.logger`: 実験記録の ABC と、固定タグを被せる decorator
- :mod:`ml.experiment.provenance`: git 由来・依存版数と、永続化境界の sanitize
- :mod:`ml.experiment.mlflow`: MLflow tracking server への adapter
"""
