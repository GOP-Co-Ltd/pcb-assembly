# src/web/ui（frontend コード）docstring・コメント 理解度テスト

## 対象
- ファイル: `src/web/ui/*.py`、`src/web/ui/static/js/**/*.js`（docstring・コメントのみ）
- 読者と用途: frontend を保守する開発者が、ページ描画・backend 中継・マシン発見・JS モジュールの役割と境界（薄いラッパー方針）を理解し、修正箇所を特定する。
- 生徒に読ませるファイル（計 1424 行）: `src/web/ui/__init__.py`, `app.py`, `pages.py`, `machines.py`, `machine_client.py`, `machines_api.py`, `static/js/app.js`

## 評価用問題
### Q1 新しい JS モジュールから、表示中の機体の backend の `GET /api/foo` を呼びたい。どの関数を使い、URL 引数には `"/api/foo"` と `"/m/<id>/api/foo"` のどちらを渡すか。`fetch` を直接呼んでよいか。
- 要点: `window.webui.api`（ファイル取得なら `downloadApi`）を使う
- 要点: `fetch` を直接呼んではいけない（`tests/web/ui/test_layout.py` が静的に検査）
- 要点: URL は `"/api/foo"` のまま渡す（機体 prefix は関数側が `withBase` で付ける）

### Q2 JS から frontend 自身の `/api/machines` を読むとき、`api()` と `frontendJson()`（`frontendApi()`）のどちらを使うか。理由も。
- 要点: `frontendJson` / `frontendApi`
- 要点: `api()` は `/m/<id>` prefix を付けるので backend へ中継され 404 になる

### Q3 frontend 自身の JSON を返す新しい APIRouter（例 `/api/foo`）を `create_app` に登録する。`pages.router` の前と後のどちらに登録するか。理由も。
- 要点: 前
- 要点: 後だと pages の `/{tab}` キャッチオールに食われる（HTML が返る）

### Q4 machine.toml の現在値を使う feature ページを新設したが、provider に渡る `_MachineSettings.fields` が空で `number()` も 503 になる。まず何を確認するか。
- 要点: `pages.py` の `_MACHINE_SETTINGS_FEATURES` にその feature slug を足したか（無い feature では取得しない）

### Q5 「どうせ使うので、全ページで `/api/settings/machine` を取得すればよい」という提案を受けた。採用しない理由は。
- 要点: backend は machine.toml をパースするので、壊れた machine.toml では 500 になる
- 要点: 全ページで取ると全画面が 503 になり、設定を直す画面すら開けない

### Q6 `_MachineSettings.number()` で backend が実効値を解決できないとき、例外ではなく 0 を返すよう変えてよいか。
- 要点: いけない
- 要点: 0 が「設定に保存」で machine.toml へ書き戻され、装置の挙動を壊す（例: canny 閾値 0 で銅箔検出が全滅）。現状は `BackendUnavailable` で 503

### Q7 SSR を速くするため、`MachineClient` の取得結果を frontend でキャッシュしてよいか。
- 要点: いけない
- 要点: frontend は backend の WS を購読しないので、他の操作者による変更で無効化する条件を書けない
- 要点: 古い値のフォームを見て書き込み操作されるほうが悪い

### Q8 mDNS だけで見つかった（machines.toml に無い）マシンの IP が DHCP で変わった。frontend を再起動しないとき、SSR ページの取得と `/m/<id>/api/**` の中継はそれぞれどの宛先へ行くか。
- 要点: SSR（`MachineClient`）は初回に作ったクライアントの古い host/port へ繋ぎ続ける（再起動まで）
- 要点: 中継（`ProxyApp`）は絶対 URL を渡すので新しい host/port へ行く

### Q9 同じ `machine_id` が machines.toml と mDNS の両方で見つかった。使われる host/port と `source` はどちらのものか。mDNS 側から取り込む値はあるか。
- 要点: host/port は静的登録（machines.toml）のもの、`source` は `"static"`
- 要点: 静的側に無い `machine_type` だけ mDNS 側で埋める

### Q10 マシン選択ドロップダウンの表示名の書式を変えたい。どこを直すか。JS 側で組み立ててよいか。
- 要点: `machines.py` の `MachineEndpoint.label`（サーバ側）
- 要点: JS で組み立てない（表示規則を JS に複製しない。JS はサーバの `label` をそのまま出す）

### Q11 backend が落ちている機体の `/m/<id>/posctrl` を開いた。何が返るか。マシン選択ドロップダウンは表示されるか。
- 要点: 「マシンに接続できません」ページ、ステータス 503（`Retry-After: 5`）
- 要点: 表示される（ドロップダウンは frontend の登録一覧から描き、backend 不要）

### Q12 操作権リース用のセッション cookie（`pcbasm_session`）は、どの応答で発行されるか。プロキシ経由の JSON 応答でも発行されるか。すでに cookie を持つブラウザがページを遷移したら、新しい値が発行されるか。
- 要点: HTML ページ応答（`_html_page`）で発行する
- 要点: プロキシ配下の JSON 応答では発行しない
- 要点: 未発行のときだけ発行する。持っていれば遷移しても新しい値は出さない（出すと操作権が自分から離れる）

## 保留問題
### H1 backend の成果物 URL `"/artifacts/x.png"` を新しいコードで `<img>` の `src` に設定する。URL をどう扱うか。
- 要点: `window.webui.withBase()` で機体 prefix（`/m/<id>`）を付けてから設定する

### H2 `config/machines.toml` が存在しないホストで frontend を起動した（mDNS でも何も見つからない）。起動は失敗するか。`/` を開くと何が出るか。
- 要点: 失敗しない（ファイル不在は空扱い）
- 要点: 「マシンが登録されていません」の案内ページ

### H3 ジョブページを開いたが、backend がそのジョブを `GET /api/jobs` で申告していない（frontend と backend の版ずれ）。どうなるか。
- 要点: 503 ページ（`BackendUnavailable`）。欠けたフォームは描かない

## ラウンド記録
### R0 執筆（生徒ファイル計 1424 行、対象全体 8455 → 約 8490 行）
- 事実誤りの修正: `machines_api` の「唯一の自前 JSON」、`machine_client` の MR4/MR5 時点の記述（mDNS 導入後の実挙動に更新）、`proxy`/`__init__` の中継範囲（`/artifacts` を追加）、`pages` の登録順コメント（`/m/{id}/{tab}` より先）、`job_console.js` の読み込み元
- 追加: `__init__` にモジュール地図、`pages` に feature 追加時の触る箇所、`app.js` に通信関数の使い分け、`viewer.js`/`paste_test_board.js` に見出しコメント
- docformatter（--wrap-descriptions=72）を作業ツリーに対して dry-run し差分 0 を確認

### R1（生徒ファイル計 1424 行。文書は変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 不正解 | 問題不良 | 要点 3（URL は `/api/foo` のまま渡す）を問題文が問うていなかった。問題文に URL の問いを足し R2 で再出題 |
| Q2-Q11 | 正解 | - | - |
| Q12 | 不正解 | 問題不良 | 要点「未発行のときだけ」を問題文が問うていなかった。問題文に遷移時の問いを足し、要点を整理して R2 で再出題 |
- 生徒の「読みにくかった箇所」: なし。文書の誤りは見つからなかったので改稿しない

### R2（生徒ファイル計 1424 行。文書は変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 正解 | - | - |
| Q12 | 正解 | - | - |
- 評価用 12 問すべて正解。次は保留問題 H1〜H3 で汎化を確認する

### 汎化確認（保留問題。文書は変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | - | - |
- 評価用 12 問と保留 3 問をすべて正解したので完了
