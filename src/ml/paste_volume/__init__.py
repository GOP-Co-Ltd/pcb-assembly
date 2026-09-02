"""画像からはんだペースト体積を推定するML subsystem.

重いML依存は各entrypointで遅延importする。このmoduleをimportしただけでは
PyTorchやONNX Runtimeを読み込まない。
"""
