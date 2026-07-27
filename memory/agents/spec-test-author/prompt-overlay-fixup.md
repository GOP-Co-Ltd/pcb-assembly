# prompt(while_waiting) レビュー追随 + overlay 既定のテスト

対象ブランチ: `fix/20260727/orthogonality-interactive`（tests/ のみ編集）

## 追加/修正したテスト

`tests/webui/jobs/test_manager.py`

- `TestPromptWhileWaiting::test_callback_exception_fails_the_job`
  — `assert record.pending_prompt is None` を追加（M1 の契約）。docstring に
  「掃除しないと終端後の job_status が pending_prompt を載せ続け、job_console.js が
  モーダルを開いたままにする」根拠を明記
- `test_abort_while_polling_prompt_marks_aborted` は既に同 assert あり（変更なし）
- クラス docstring に「resolved でない脱出（例外・abort）でも pending prompt は掃除される」を追記
- `test_callback_is_not_called_after_the_answer` の docstring を実際に固定している範囲へ
  書き換え（N1）。強化はしない — 決定的に強めるには「応答が submit された時点」を
  コールバック側から観測する必要があり、in-flight のコールバックと区別できないため
  偽陽性の窓が残る。同期を足す複雑さに対して得るものが小さい

`tests/webui/routers/test_pages.py`

- module docstring に overlay 既定の契約（`preview_overlay`、既定 none、ツアー系のみ crosshair）
- `_checked_overlay(html)` ヘルパ（overlay ラジオのうち checked が付いた value を返す）
- `TestPosctrlJobPages::test_posctrl_job_page_default_overlay`
  — board_tour / orthogonality_test → crosshair、camera_calibration → none
- `TestPreviewPages::test_camera_preview_page_defaults_overlay_to_none`

`tests/helpers.py` / `test_manager.py`

- 語中改行由来の全角空白を解消（N3）。docformatter は 72 桁で日本語も空白位置で
  折り返すため、**説明文は 1 段落 1 行・70 桁未満**に収める形にした（長い段落は
  再 wrap で必ず語中に空白が入る）

## 検証

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（1617 passed / 88 deselected）
- make test-e2e: pass（52 passed）

実装（manager の try/finally、preview_controls.html / posctrl/job.html の
`preview_overlay`）は合流済みで、追加テストは全て緑。実装側への追加要求なし。
