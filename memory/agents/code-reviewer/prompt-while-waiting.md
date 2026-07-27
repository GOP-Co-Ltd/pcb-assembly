# prompt(while_waiting=...) レビュー

対象: `fix/20260727/orthogonality-interactive` の未コミット変更（src 4 / tests 3）

## verdict: request-changes

must-fix 1 件（`while_waiting` 例外時の pending prompt 残留）。他は設計判断と nit。

## must-fix

### M1. `while_waiting` の例外で pending prompt が record に残り、終端後もモーダルが開いたままになる

- 対象: `src/webui/jobs/manager.py:300-311`
- 問題: ポーリングループから例外が抜けると `self._pending = None` と
  `record.set_pending_prompt(None)` を通らずに propagate する。ジョブは FAILED に
  なるが record は pending prompt を保持し続ける。
- 根拠（実測: 一時テストで確認、実行後削除済み）:
  - `record.status == failed` かつ `record.pending_prompt` が非 None
  - `webui.routers.jobs.job_summary()` が `pending_prompt` を載せる（`routers/jobs.py:63`）
    → REST `/api/jobs/current` と WS `job_status` の両方に出る
  - `job_console.js:365` の `renderConsole()` は status を見ずに
    `openPrompt()` → `dialog.showModal()`。FAILED の結果パネルはモーダル背後に隠れる
  - `manager.respond_prompt()` は死んだ worker の pending に**エラーなく成功**する
    （押しても何も起きない。2 回目は ValueError）。リロードすると再度モーダルが開き、
    `jc-abort` は終端で無効なので閉じ切れない
- 新規性: 変更前の `prompt()` は「resolved」「abort」以外の脱出経路が無く、どちらも
  掃除を通っていた。よって本変更で新規に生じた挙動（plan-implementer メモの
  「本変更で新たに生じた挙動ではない」は誤り）
- 発生条件: 待機中の `result.camera.capture()` が例外（FrameHub キャプチャスレッド
  死亡時の保持例外の再送出、または subscribe 既定 5s のタイムアウト）。直行性テストは
  1 点あたり数十秒〜数分待つので露出時間が長い
- 直すべき点: 待機を try/finally 化（非 resolved 脱出時に必ず掃除）。掃除すれば終端時の
  `job_status` が `pending_prompt=None` を運び、JS が `closePrompt()` して
  エラー内容が見えるようになる
- 併せて `tests/webui/jobs/test_manager.py:479` 付近に
  `assert record.pending_prompt is None` を追加して契約を固定する
- 確信度: 高（実測）／深刻度: 中（エラー経路だが UI が行き止まりになる）

## should-fix

### S1. プレビュー更新失敗でジョブ全体が FAILED になる露出窓が桁違いに拡大した

- 対象: `src/webui/jobs/posctrl.py:394-403` + manager の例外素通し
- 変更前はカメラ例外の露出窓が `_stream_labeled_frames` の 1 秒だけ。今は prompt 待機の
  全期間（ユーザーがベルト調整している数分）。直行性テストの setup は実 Klipper の
  ホーミング + 基準点合わせを要するため FAILED からの復帰コストが高い。プレビューの
  一時的な取得失敗でツアーを落とす価値があるかは要判断（`submit` 内で例外を握って
  `ctx.log` 1 回に留める選択肢）
- 付随: abort 要求がカメラのストール中に来ると in-flight の `capture()` が 5s 後に
  TimeoutError を投げ、ABORTED ではなく FAILED になる
- 確信度: 中（設計判断・動作は仕様どおり）／深刻度: 中

### S2. `PROMPT_POLL_SEC` が public だが利用者がいない

- `src/webui/jobs/manager.py:47`。同モジュールの他の内部定数は `_ABORT_SENTINEL` /
  `_PCBASM_LOGGER_NAME` と private。テストからも import されていない（grep 済み）
- 確信度: 高／深刻度: 低

## nit

- N1. `test_manager.py::test_callback_is_not_called_after_the_answer`（440 行付近）は
  現実装では落ちない。`events` は worker 単一スレッドが順に append するため
  「`answered` 以降に `first-poll` が無い」は構造的に必ず成立し、docstring が名指しする
  回帰（応答検知前にもう 1 回呼ぶ実装）は `answered` の append より前に起きてすり抜ける。
  実質固定できているのは「別スレッドでポーリングし続ける実装ではない」ことだけ。
  意図をその範囲に書き直すか削る。確信度: 中／深刻度: 低
- N2. 移動中（`_move_to`）はフレームを出さないので override が 1s TTL で失効し、
  ラベルなしのページ側 overlay に戻る。変更前も同じで回帰ではないが「常に表示」の
  要求に対しては移動中だけ穴が残る。確信度: 高／深刻度: 低
- N3. `test_manager.py:440` 付近の docstring が語中改行で「十分な ポーリング周期」と
  不自然な空白になる（docformatter 由来。既存 `tests/helpers.py:32` にも同様）

## 確認して問題なかった点

- **要求達成**: production 設定は camera fps=30 / override TTL=1.0s。1 ポーリング周期
  ≒ 0.05（poll）+ 0.033（新フレーム待ち）+ 描画 ≒ 0.09s で TTL に 10 倍の余裕。
  TTL 内更新が破れるのは実効 fps が ~1.06 未満に落ちたときだけで、1 コマ欠落に留まる
- **カメラ二重取得なし**: `FrameHub` はキャプチャスレッド 1 本だけが `camera.capture()`
  を呼び、消費者はカーソル + Condition 待ち（`framehub.py:106-132`）。PreviewService と
  job は別カーソルで互いのフレームを奪わない
- **後方互換**: `while_waiting is None` で `pending.event.wait()` に落ちる（従来と同一）。
  他の prompt 利用箇所（pasting 5 / posctrl 1 / dev 2 / board_ops 1）は spec のみの
  呼び出しで無影響
- **abort / スレッド安全**: `abort()` は `_pending_lock` 下で event set（`manager.py:369`）。
  ポーリングは次の `wait(0.05)` で抜けて非 resolved 経路へ合流。`shutdown` も畳める
- **過剰設計ではない**: `persist=True` は静止画固定でライブ + 十字を満たせない。代替
  （ワーカー内ストリーミングスレッド / PreviewService へジョブ提供レンダラ）はいずれも
  本変更（optional kwarg 1 個・既定で従来動作）より大きい
- 成果物汚染（`</content>` 等）なし

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（1613 passed, 88 deselected）
- 新規テストのみ 5 回連続 pass（8 passed / 0.77s、random order 有効）
