"""Backend（web.api）、frontend（web.ui）、自己更新（web.selfupdate）が共有する契約.

pydantic と標準ライブラリだけに依存する（`tests/web/contract/test_models.py`）。
機体と frontend の版がずれても読めるよう、契約を変えたら `discovery.API_VERSION` を上げる。
"""
