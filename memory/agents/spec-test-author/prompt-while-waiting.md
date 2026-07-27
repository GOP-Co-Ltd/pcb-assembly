# prompt(while_waiting=...) の仕様テスト

対象ブランチ: `fix/20260727/orthogonality-interactive`（tests のみ担当）

## 固定した契約

`JobContext.prompt(spec, *, while_waiting=None)` / `board_ops.confirm_next_point(ctx, label, *, while_waiting=None)`
（直行性テスト中にプレビューの十字線・ROI・ラベルを待機中ずっと表示し続けるための経路）。

- 応答が来るまで `while_waiting` がポーリング間隔（`manager.PROMPT_POLL_SEC`）ごとに
  繰り返し呼ばれ、返り値は `while_waiting` 無しと同じ
- 応答後は呼ばれない（poll ループが応答で終了する）
- ポーリング待機中の abort は `JobAborted` → ジョブ ABORTED（`while_waiting` 無しと同型）
- `while_waiting` の例外は握りつぶさずジョブ FAILED（error に例外メッセージ）
- `confirm_next_point` は受け取った `while_waiting` を `ctx.prompt` へそのまま委譲する
- `while_waiting=None`（既定）の従来挙動は既存 `TestPrompt` が担保済みなので重複させない

## 追加したテスト

`tests/webui/jobs/test_manager.py` — 新クラス `TestPromptWhileWaiting`
（ヘルパ `_register_polling_prompt` を追加）:

- `test_callback_is_called_repeatedly_until_the_answer_arrives`
- `test_callback_is_not_called_after_the_answer`
- `test_abort_while_polling_prompt_marks_aborted`
- `test_callback_exception_fails_the_job`

`tests/webui/jobs/test_board_ops.py` — `TestConfirmNextPoint`:

- `test_while_waiting_callback_runs_during_the_wait`

`tests/webui/jobs/test_posctrl.py` — docstring のみ同期（モジュール docstring と
`TestPosctrlHardware`）。実機の目視確認項目として「待機が何秒続いてもオーバーレイが
消えないこと」を明記。実機テスト本体は変更なし。

## 決定的同期の作り方（レビュー時の注目点）

固定 sleep でタイミングを待たない設計にした:

- 「繰り返し呼ばれる」は `wait_until(lambda: len(polls) >= 2)` で観測してから応答する
- 「応答後は呼ばれない」は 2 つ目の prompt に別コールバックを付け、
  `wait_until(lambda: events.count("second-poll") >= 3)` で「応答後に複数ポーリング
  周期が経過した」ことを観測イベントで保証し、`events` の順序で
  `"answered"` 以降に `"first-poll"` が無いことを assert する

## 状態

実装（`context.py` / `manager.py` / `board_ops.py` / `posctrl.py`）は並列で着地済み。
`make format` / `make type` / `make test-no-hardware` すべてグリーン（1613 passed）。
新規テストは `-p no:randomly` で 3 回連続グリーン（flaky なし）。実装側への修正要求なし。
