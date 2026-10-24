"""machine.toml の冪等な移行.

backend は設定を読む前に :func:`.machine_toml.migrate_machine_toml` を呼ぶ。
移行が要らないファイル（移行済み・新しいテンプレートから作ったもの）には何もしない。
``python -m pcbasm.migrate --check PATH`` は書き換えずに差分だけを表示する。

移行のステップは、両機が移行を含む版を越えてから削除する。
"""
