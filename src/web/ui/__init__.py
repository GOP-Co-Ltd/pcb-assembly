"""PCB アセンブリ装置の UI frontend (FastAPI).

ブラウザ向けのページと静的資産を配信し、``/m/{machine_id}/api`` 以下を各マシンの
backend WebAPI (``web.api``) へ中継する。装置設定 (machine.toml) やカメラを一切
持たないため、機体でない PC でも単独で起動できる。表示する値はすべて backend から
取得する。
"""
