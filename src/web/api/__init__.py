"""PCB アセンブリ装置の backend WebAPI (FastAPI).

各マシン（機体）で動き、装置制御・カメラ・設定ファイル・ジョブ実行をそのマシンに閉じて提供する。
UI の配信は ``web.ui`` の担当。

エンドポイントを守る仕組みは 2 つあり、別物である。

- 操作権（``control.ControlLease``）: 誰が指示を出せるか。変更系のエンドポイントは ``ControlDep`` を引数に取り、保持者以外を 423 で断る。
- 装置排他ロック（``AppState.machine_lock``）: 装置や設定を今いじっている処理が 1 つだけであること。取れなければ 409 で断る。ジョブは実行中ずっとこのロックを持つ。

次の操作は安全のため、または読み取りだけなので、操作権を要求しない。

- 緊急停止（``POST /api/emergency-stop``）とジョブの中止（``POST /api/jobs/current/abort``、WS の ``abort``）
- 操作権の奪取（``POST /api/control/takeover``）
- GET 全般と、装置を動かさない計算用の POST

例外から HTTP ステータスへの変換は ``app.create_app`` の例外ハンドラと ``routers.common.klipper_errors_to_502`` に集まっている。
"""
