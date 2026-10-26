"""PCB アセンブリ装置の UI frontend (FastAPI).

ブラウザ向けのページと静的資産を配信し、``/m/{machine_id}/api`` と ``/m/{machine_id}/artifacts`` を各マシンの backend WebAPI (``web.api``) へ中継する。
装置設定 (machine.toml) やカメラを一切持たないため、機体でない PC でも単独で起動できる。
表示する値はすべて backend から取得し、frontend では再計算しない。

モジュールの役割:

- ``app``: アプリの組み立てとルートの登録順
- ``pages``: SSR ページ（backend から取った値をテンプレートへ渡す）
- ``features``: タブ / feature / テンプレートの機能表
- ``layout``: ジョブフォームのまとまりと設定ページの画面・カードへのまとめ方
- ``proxy``: ``/m/{machine_id}/api`` と ``/m/{machine_id}/artifacts`` の中継（http と WebSocket）
- ``machines`` / ``discovery``: マシン登録（``config/machines.toml``）と mDNS 探索
- ``machine_client``: SSR 用の backend HTTP クライアント
- ``machines_api`` / ``update_api``: frontend 自身が返す JSON（``/api/machines`` / ``/api/self-update/**`` / ``/api/update-notice``）と ``/update`` ページ
- ``templates/partials/forms.html``: ジョブのパラメータと machine.toml の設定項目の入力欄（全ページ共用のマクロ）
- ``static/js/api.js`` / ``ws.js``: 全ページ共通の HTTP と保存待ち、WS のクライアント（backend への通信はここを必ず通す）
"""
