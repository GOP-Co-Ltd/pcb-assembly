"""ペースト塗布の制御.

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import する
（例: ``from pcbasm.pasting.applicator import PasteApplicator``）。
``import pcbasm.pasting`` 自体は cv2 / pcbnew / torch などの重い依存を読み込まない。
"""
