# レビュー指摘の適用（prompt(while_waiting=...) の後始末・プレビュー耐障害化・overlay 既定）

`memory/agents/code-reviewer/prompt-while-waiting.md` の M1 / S1 / S2 + 「ページ overlay 既定」を適用した。

## 計画外の判断ログ

- **M1**: `try/finally` ではなく `try/except BaseException: ... raise` にした。abort 判定
  （`if not pending.resolved: raise JobAborted()`）を try の中に入れると「未解決で抜ける」
  条件が例外の有無と一致し、`if not pending.resolved` の二重評価を避けられる。
  掃除は新設した private ヘルパー `_JobRuntime._discard_pending(pending)` に集約
  （runtime の `_pending` を identity 一致時のみ None にし、`record.set_pending_prompt(None)`）。
  resolved 経路も同じヘルパーを通す。abort 経路の発行イベントは従来どおり
  （`publish_status()` → `JobAborted`）で、`while_waiting` 例外経路も同じ掃除 + `publish_status()`
  を通ってから例外を再送出する（ジョブは FAILED のまま）。
- **S1**: `_labeled_frame_sink` の `submit` 内で `except Exception` し、クロージャの
  `warned` フラグで最初の 1 回だけ `ctx.log` する。sink は巡回先 1 点ごとに作られるため
  「同じ待機中に同じ行を吐かない」が成立し、点が変われば再度警告できる。捕捉範囲は
  `capture + render + ctx.frame` の 1 式のみ。`manager.py` 側の「`while_waiting` の例外は
  素通し（FAILED）」契約は変更していない。
- **S2**: `PROMPT_POLL_SEC` → `_PROMPT_POLL_SEC`（参照は同モジュール内 1 箇所のみ）。
- **overlay 既定**: `preview_controls.html` で `{% set selected_overlay = preview_overlay |
  default('none') %}` を置き、各ラジオに `{{ 'checked' if selected_overlay == '<value>' }}`。
  `posctrl/job.html` に `{% set preview_overlay = "crosshair" %}`（`copper_detection.html` と
  同じパターン）。`preview_controls.html` の include 元は camera_preview / camera_calibration /
  posctrl/job の 3 ページのみで、前 2 つは既定 `none` のまま。JS / CSS は無変更。

## 他implementerへのIF変更通知（並列時）

- `webui.jobs.manager.PROMPT_POLL_SEC` は **`_PROMPT_POLL_SEC` に改名**（private 化）。
  外部からの import は禁止。以前のメモにある「public 定数。テストから import 可」は無効。
- 公開シグネチャ（`JobContext.prompt` / `JobBridge.prompt` / `_JobRuntime.prompt` /
  `confirm_next_point`）は不変。
- テンプレート変数 `preview_overlay` が `preview_pane.html` に加えて
  `preview_controls.html`（ラジオの初期選択）にも効くようになった。

## 既知の制約・残課題

- `_labeled_frame_sink` の例外握りは private 関数の内部挙動なので直接テストしていない
  （公開 IF ではないため方針どおり）。カバーしたい場合は `_run_orthogonality_test` が
  実 Klipper を要求するため hardware 区分の目視確認になる。
- 点間の移動中（`_move_to` の完了待ち）はジョブがフレームを出さず override が TTL 1 秒で
  失効するが、ページ既定が `crosshair` になったため MJPEG 側の十字線が引き継ぐ。
  ラベル・ROI は移動中は出ない（設計どおり）。

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（1617 passed, 88 deselected）
- make test-e2e: pass（52 passed, 1653 deselected）
