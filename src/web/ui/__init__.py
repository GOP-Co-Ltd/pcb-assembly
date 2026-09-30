"""PCB アセンブリ装置の UI frontend (FastAPI).

ブラウザ向けのページと静的資産を配信し、``/m/{machine_id}/api`` と ``/m/{machine_id}/artifacts`` を各マシンの backend WebAPI (``web.api``) へ中継する。
装置設定 (machine.toml) やカメラを一切持たないため、機体でない PC でも単独で起動できる。
表示する値はすべて backend から取得し、frontend では再計算しない。

モジュールの役割:

- ``app``: アプリの組み立てとルートの登録順
- ``pages``: SSR ページ（backend から取った値をテンプレートへ渡す）
- ``layout``: タブ / feature / テンプレート / 設定セクションの表示知識
- ``proxy``: ``/m/{machine_id}/api`` と ``/m/{machine_id}/artifacts`` の中継（http と WebSocket）
- ``machines`` / ``discovery``: マシン登録（``config/machines.toml``）と mDNS 探索
- ``machine_client``: SSR 用の backend HTTP クライアント
- ``machines_api`` / ``update_api``: frontend 自身が返す JSON（``/api/machines`` / ``/api/self-update/**`` / ``/api/update-notice``）と ``/update`` ページ
- ``static/js/app.js``: 全ページ共通の JS（backend への通信はここの関数を必ず通す）
"""
