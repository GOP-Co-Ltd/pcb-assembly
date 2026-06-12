# WebUI Phase 3: ジョブ実行基盤 + dev タブ（実装ログ）

計画書: `memory/agents/implementation-planner/webui-phase3.md`
担当: レーン A（pcbasm 昇格）/ B（webui ジョブ基盤）/ C（UI）

## 実装サマリ

計画書の公開 IF・WS スキーマどおりに実装。逸脱なし（下記「計画との差分」は
すべて内部実装または計画が裁量に委ねた範囲）。

- レーン A: `src/pcbasm/visualization/`（patches / pcb_render / fill_render +
  `__init__` re-export）、`src/pcbasm/pcb/generate.py`（generate_grid_pcb /
  build_fill_coverage_board / save_board）、scripts 4 本を薄い CLI ラッパ化
- レーン B: `src/webui/jobs/`（context / catalog / manager / dev）、
  `src/webui/routers/jobs.py`（REST + WS /api/ws）、`AppState.acquire_machine`
  / `release_machine`（machine_lock はその上に再実装）、models.py に
  JobSummary 系追加、app.py（catalog/jobs 構築・lifespan bind_loop/shutdown・
  /artifacts StaticFiles マウント）、既存ルーター変更（state_changed 発行・
  E-STOP abort 連動・stage/limits・gcode アクション・/api/state の job ブリーフ）
- レーン C: `templates/dev/job.html`（ParamSpec からフォーム導出）/
  `dev/klipper_status.html` / `partials/job_console.html`、
  `static/js/job_console.js`（WS クライアント + コンソール、
  `window.webui.jobs` 公開）/ `klipper_status.js` / `machine_control.js` の
  ジョブモード切替、app.css 追記
- `data/.gitignore` に `webui/` を追記（成果物がコミット対象にならないように）

## 計画との差分・内部実装の判断（IF 変更通知に該当するものは無し）

1. **JobContext の橋渡しは Protocol（`JobBridge`）経由**。manager.py の
   `_JobRuntime` が実装し、JobContext は委譲のみ。pyright
   reportPrivateUsage warning ゼロ維持のため（Phase 2 の先例踏襲）。
   JobContext の公開 IF は計画どおり。
2. **JobRecord の更新メソッド（set_status / append_log 等）は非 underscore**。
   計画は「公開 read プロパティ + JobManager が更新」とだけ規定。
   reportPrivateUsage 回避のため更新系を通常メソッドにし、docstring で
   「JobManager 内部専用」と明示。read プロパティは計画どおり全部ある。
3. **WS の job_status は送信時に最新サマリへ enrich**。manager は
   `{"type":"job_status","job_id":...}` を発行し、`_send_loop` が
   `job_summary(jobs.current(), catalog.get(...))` で本文を構築（変換点は
   計画どおり routers/jobs.py の `job_summary` 1 箇所）。旧ジョブの遅延
   イベント（job_id 不一致）は捨てる。遷移ごとにイベントは 1 件出るため
   受信駆動テストは決定的（spec-test-author のテストで検証済み）。
4. **abort の prompt/next_command 起こし**: prompt は resolved=False のまま
   event.set()、command キューは `_ABORT_SENTINEL` 投入。即時性はポーリング
   ではなくイベント駆動で実現。
5. **WS の error 応答は購読キュー経由**。受信ループから直接 send せず
   `events.put_nowait({"type":"error",...})`（送信タスクへの一本化。
   2 タスク同時 send の競合回避）。
6. **`POST /api/jobs/last/apply` 成功後に `publish_state_changed()` を発行**
   （計画に明記なし）。設定変更の一種なので spec §6 の state_changed 意味論に
   合わせた。クライアントはヘッダ表示を取り直すだけで害なし。
7. **klipper_status の JS は `static/js/klipper_status.js` に分離**（計画の
   static 一覧には無いが、preview.js / settings.js のページ別 JS パターンに
   合わせた。インライン script より CSP/整形に優しい）。
8. **generate_grid_pcb の print は昇格後も維持**（計画は build_fill_coverage
   側のみ「print を持ち込まない」と規定）。spec-test-author は print を契約に
   含めない判断（テストメモ参照）なので、将来消すのは自由。
9. **machine_control.js のパネル disabled 復帰**はテンプレート由来の
   disabled（フォーカスZ未設定）を `originallyDisabled` セットで保持して維持。

## spec-test-author との突合せ

`memory/agents/spec-test-author/webui-phase3.md` 確認済み。
「期待される失敗 / 実装側に求める修正: なし」。テスト側の判断
（fill_coverage は 8 pads = 既存 fixture と一致、print 非契約化）も実装と整合。
テストファイルは一切編集していない。

## 検証結果

- `make format`: 全フックパス
- `make type`（pyright src+tests）: 0 errors / 0 warnings
- `uv run pytest -m "not hardware" -q`: **874 passed, 23 deselected**
  （tests/webui/jobs・routers/test_jobs.py 含む。hardware 区分はユーザー実行待ち:
  stage/limits 実機 200・gcode M400 実機 200 の 2 件 + 既存分）
- 自前スモーク（TestClient）: extract_pcb SUCCEEDED + artifacts 5 件 +
  PNG cv2 可読 / traversal 404 / 未知ジョブ 404 / パラメータ不正 400 /
  実行中 二重 start・machine-control・PUT machine = 409 / WS で
  log/progress/prompt 往復/error（id 不一致）/abort/command エコー→quit /
  apply で test-fixture コピーの machine.toml に canny_low 書込 + 二重 apply
  409 / E-STOP（Moonraker 不通 502）でジョブ ABORTED / 新ジョブ開始で旧
  成果物ディレクトリ削除
- 実 uvicorn E2E（FakeCamera, port 8099, data_dir=/tmp）: dev 4 ページ +
  klipper_status のマーカー OK / extract_pcb 実行 → succeeded → /artifacts の
  PNG を cv2 復号 OK（1500x1800）/ websockets ライブラリで WS 通し
  （prompt 往復 → succeeded + apply_available, abort → aborted）OK /
  実行中ジョブを残したまま SIGTERM → 5 秒以内に graceful shutdown 完了
  （lifespan の jobs.shutdown が機能）
- ブラウザでの体感確認（フォーム→コンソール・プロンプトモーダル・PNG
  インライン・Apply ボタン）と実 Moonraker での G-code 送信・stage limits は
  **ユーザー実施待ち**（計画書どおり）

## docs-keeper への引き継ぎ（計画書リスク 6 の再掲）

spec §6/§9 への追記事項: WS `error` イベント / `POST /api/jobs/last/discard` /
`GET /api/stage/limits` / machine-control `gcode` アクション / hidden ジョブ
`job_demo` / 成果物の自動削除ポリシー（新ジョブ開始で前回分削除）/
apply 成功時の state_changed 発行（本実装の判断 6）。
