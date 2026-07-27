# prompt(while_waiting=...) による直行性テストのオーバーレイ常時表示

## 計画外の判断ログ

計画書どおり実装した。逸脱は 1 点のみ（追加ヘルパーの導入）。

- `posctrl.py` に `_labeled_frame_sink(ctx, result, label) -> Callable[[], None]`
  をモジュールプライベート関数として追加した。`_run_orthogonality_test` の
  ループ内に直接 `def` を書くとループ本体が縦に伸びて `confirm_next_point`
  呼び出しとの対応が読みにくいため、既存の `_stream_labeled_frames` /
  `_stream_pad_result` と同じ階層に factory を置いた。描画内容は
  `_stream_labeled_frames` の 1 枚分と同一（`render_label(camera.capture(),
  crop.size, label)` を `ctx.frame` へ）。
- `while_waiting` 内で `ctx.checkpoint()` は呼んでいない。`_JobRuntime.abort()`
  が `pending.event.set()` するので待機ループは自然に抜け、既存の
  `if not pending.resolved: raise JobAborted()` 経路に合流する。checkpoint を
  足すと JobAborted の発生箇所が二重化するだけで得がない。

## 他implementerへのIF変更通知（並列時）

計画書のシグネチャから逸脱なし。確定形:

- `JobBridge.prompt(self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None) -> Answer`
- `JobContext.prompt(self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None) -> Answer`
- `_JobRuntime.prompt(self, spec: PromptSpec, *, while_waiting: Callable[[], None] | None = None) -> Answer`
- `confirm_next_point(ctx: JobContext, label: str, *, while_waiting: Callable[[], None] | None = None) -> bool`
- `webui.jobs.manager.PROMPT_POLL_SEC = 0.05`（public 定数。テストから import 可）

## 既知の制約・残課題

- `while_waiting` の呼び出し間隔は `PROMPT_POLL_SEC` を**下限**とするだけで、
  コールバック自体の所要時間（カメラ 1 フレーム取得 + 描画）が加算される。
  実効フレームレートはカメラ側が支配する。
- `while_waiting` が投げた例外は素通しで、ジョブは FAILED になる（カメラ切断を
  隠さない仕様）。このとき `record.set_pending_prompt(None)` は通らないため
  pending prompt が record に残るが、ジョブは終端ステータスへ移るので UI は
  プロンプトを出したままにしない。既存の abort 経路以外の例外でも同じ状態に
  なるため、本変更で新たに生じた挙動ではない。
- `_stream_labeled_frames` は `_run_board_tour` の四隅巡回が使い続けるため残置。
  `RESULT_DISPLAY_SEC` も引き続き使用中。

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（1608 passed, 88 deselected）
