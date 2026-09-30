# src/web/api/ docstring・コメント 理解度テスト

## 対象
- ファイル: `src/web/api/` 配下の全 .py（docstring・コメント・API 説明文字列のみ）
- 読者と用途: backend を保守する開発者と API 利用者。各 router・ジョブ・操作権・設定 API の役割と契約を理解し、正しく呼び出し・修正する
- 生徒に読ませるファイル（計 1453 行、R1 時点）:
    - `src/web/api/__init__.py`
    - `src/web/api/control.py`
    - `src/web/api/dependencies.py`
    - `src/web/api/routers/control_api.py`
    - `src/web/api/routers/jobs.py`
    - `src/web/api/routers/settings_api.py`
    - `src/web/api/routers/system.py`
    - `src/web/api/routers/machine_control.py`
    - `src/web/api/routers/update.py`
    - `src/web/api/jobs/catalog.py`

## 評価用問題
### Q1 操作権が必要な新しい変更系エンドポイントを router に追加する。操作権の検証をどう書くか。ハンドラ本体で `lease.claim()` を呼んではいけない理由も答えよ。
- 要点:
    - ハンドラ引数に `ControlDep`（`Depends(require_control)`）を取る
    - 本体で claim すると `klipper_errors_to_502()` が `ControlDeniedError`（RuntimeError 派生）を巻き込み、拒否が 423 ではなく 502 になる

### Q2 ファームウェア再起動（`POST /api/firmware-restart`）と緊急停止（`POST /api/emergency-stop`）は、それぞれ操作権が必要か。理由も答えよ。
- 要点:
    - ファームウェア再起動は必要（復帰操作）
    - 緊急停止は不要（安全機能。誰でも常に止められる必要がある）

### Q3 変更系 API を呼んだら 423 が返った場合と 409 が返った場合、それぞれの原因と、クライアントが次にすべきことを答えよ。
- 要点:
    - 423: 他のクライアントが操作権を保持している → 操作権を取得（acquire）するか、必要なら奪取（takeover）する
    - 409: 装置排他ロックが取られている（ジョブ実行中など装置が使用中）→ 終わるのを待ってやり直す（操作権の奪取では解消しない）

### Q4 `POST /api/machine-control` のハンドラで、`state.machine_lock(...)` と `klipper_errors_to_502()` のどちらを外側に置くか。理由も答えよ。
- 要点:
    - `machine_lock` を外側に置く
    - `BusyError` は RuntimeError 派生なので、内側で投げると 502 に変換されてしまい、409 のハンドラへ届かない

### Q5 操作権の保持者が長時間のジョブを開始し、その後 15 分間何も操作しなかった（ブラウザは開いたまま）。操作権は無操作で失効するか。
- 要点:
    - 失効しない
    - ジョブ実行中（`busy()` が True）は無操作タイムアウト（既定 600 秒）による失効をしない

### Q6 操作権を持たないクライアントが WebSocket `/api/ws` で `command` メッセージを送った。サーバーはどう応答するか。WebSocket 接続は切れるか。
- 要点:
    - `error` イベント（type: error）で断る（HTTP 423 ではない）
    - 接続は切れない

### Q7 `POST /api/jobs/{name}/param-defaults` に、正しい値と一緒に、`persisted_params` に含まれないキーや型の合わない値を混ぜて送った。正しい値と不正な値は、それぞれどう扱われるか。
- 要点:
    - それらの値は黙って無視される（エラーにならない）
    - 残りの型整合する persisted_params の値だけが保存済み既定値にマージされる

### Q8 `PUT /api/jobs/current/params` に `expected_job_id` を付けるのは何のためか。一致しなかったときのステータスコードも答えよ。
- 要点:
    - 画面が見ていたジョブ以外（切り替わった後の別ジョブ）を誤って更新しないため
    - 不一致は 409

### Q9 `PUT /api/settings/machine` で保存したとき、カメラが再構築されるのはどんな場合か。
- 要点:
    - `camera.` で始まるキーを含む場合
    - `camera.crop.*` だけの変更では再構築しない（ストリームを切らない）

### Q10 ソフトウェア更新 API に、更新元のブランチ名をリクエストで指定できるパラメータを足してよいか。
- 要点:
    - 足してはいけない
    - 参照先をリクエストで指定できると LAN から任意コードを実行できるようになる（参照先はサーバー側の固定値）

### Q11 装置を動かさないジョブ（PCB 生成など）を登録するとき `uses_machine=False` にする。これで何が変わるか。
- 要点:
    - 終了時のノズルキャップ駐機をしない
    - 機体スピーカーの完了通知音を鳴らさない

### Q12 frontend が「自分が操作権の保持者か」を判定するには、何と何を比べるか。
- 要点:
    - `GET /api/state`（または `/api/control/*` の応答）の `control.key` と `you.key` を比べ、一致すれば保持者
    - WS の `control_changed` を受けたときも、その `control.key` を自分の `you.key` と比べる（サーバーは接続ごとに作り分けない）

## 保留問題
### H1 操作権の保持者がブラウザのタブを閉じ、WebSocket 接続がすべて切れた。操作権はいつ解放されるか。
- 要点:
    - 接続数が 0 になってから `disconnect_grace`（既定 30 秒）を超えたら失効する
    - 失効はバックグラウンドではなく、次にリースが呼ばれたときに判定される（lazy）

### H2 `uses_machine=False` のジョブが実行中に `PUT /api/settings/machine` を呼んだ。どうなるか。
- 要点:
    - 409 で断られる
    - ジョブは `uses_machine` に関係なく実行中ずっと装置排他ロックを持つため

### H3 別のクライアントが操作権を奪取（takeover）した。元の保持者が開始して実行中のジョブはどうなるか。
- 要点:
    - 何も変わらず実行を続ける
    - 奪取で移るのは指示を出す権利だけで、走っている処理には触らない

## ラウンド記録
### R0（執筆。対象全体 12520 → 12556 行）
- docformatter で文の途中が改行・空白で壊れていた docstring を 1 文 1 行に直した（control / system / jobs / control_api / common / preview / fake_camera / posctrl / dataset_finalize / pasting_view）
- `default_catalog` の「pasting 6 ジョブ」は実物（9 ジョブ）と矛盾していたため、件数を書かない表現に変えた
- docstring の無かったエンドポイント 8 件（`GET /state`・`GET /files`・`PUT /pcb-file`・`POST /machine-control`・`GET /preview/stream`・`GET/PUT /settings/machine`・`GET /klipper/status`）に役割とステータスコードを書いた（FastAPI の description に出る）
- `web/api/__init__.py` に「操作権（423）と装置排他ロック（409）は別物」「ゲートしない操作」の概要を足した（各 router に散っていた契約の入口）
- WS の docstring に、操作権が要るメッセージと error イベントでの拒否を明記した

### R1（生徒に読ませるファイル 1453 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 正解 | - | - |
| Q2 | 正解 | - | - |
| Q3 | 正解 | - | - |
| Q4 | 正解 | - | - |
| Q5 | 正解 | - | - |
| Q6 | 正解 | - | - |
| Q7 | 不正解 | 問題不良 | 正しい値がマージ保存されるという要点が欠けていた。ただし問題文は不正な値のことしか聞いておらず、正しい値の扱いを問うていなかった。docstring（`post_job_param_defaults` の「型整合する値だけを既存の保存済み既定値へマージする」）は明確なので、文書は変えずに、両方の扱いを問う文に直した |
| Q8 | 正解 | - | - |
| Q9 | 正解 | - | - |
| Q10 | 正解 | - | - |
| Q11 | 正解 | - | - |
| Q12 | 正解 | - | - |
- 生徒の「読みにくかった箇所」はなし

### R2（生徒に読ませるファイル 1453 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q12 | 全問正解 | - | - |
- Q7 は直した問題文で正解した（正しい値は保存、それ以外は黙って無視）
- 全問正解なので、次は保留問題 H1〜H3 で汎化を確認する

### 汎化確認（保留問題、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | - | - |
- 3/3 正解。H2 では、操作権が無い場合の 423 も正しく区別できていた
- ループを完了した。最終状態は対象全体 12556 行、生徒に読ませるファイル 1453 行
