"""ONNX 成果物の生成・検査・採否判定.

学習済み model を ONNX へ出し、graph を検査し、static INT8 候補を作る。

eager との一致と実機 latency を測り、その証拠から promote 可否を決める。

module は必要な依存の重さで分かれている。

``manifest`` と ``promotion`` は追加依存なしで import できる。

``runtime`` と ``benchmark`` は onnxruntime と numpy だけで import できる。

``parity`` は torch と onnxruntime を要求する。

``graph`` と ``onnx_export`` と ``quantization`` は onnx を要求する。

この package は再 export しない。

import 文だけで、どの依存を引き込むかを判別できるようにするため。
"""
