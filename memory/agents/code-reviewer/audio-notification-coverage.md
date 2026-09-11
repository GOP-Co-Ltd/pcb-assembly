# 通知音の発火条件を再設計する（audio-notification-coverage）レビュー

## verdict: request-changes

must-fix 1 件（仕様漏れ）。方針 1〜3 自体の実装は概ね正確で、diff は要求範囲に収まっている。

## must-fix

### 1. `reference_point_setup` のジョグ待機が通知されない

- 対象: `src/web/api/jobs/posctrl.py:180-186`（`_run_reference_point_setup`）
- 問題: `ctx.progress("ジョグ待機")` / `ctx.log("マシン操作パネルでジョグし、Record で
  現在位置を記録してください")` の直後に入るループが、明白なオペレータ待ちなのに
  `ctx.notify_operator()` を呼んでいない。
- 根拠: 方針 1「オペレータ待ちに入るたび必ず鳴らす」。実装者の除外基準
  （計画書「`posctrl.py:184` と `common.drain_commands` は `timeout=0` のポーリングなので
  対象外」）は *待ちの実装機構* による基準で、仕様の *待ちの意味* による基準とずれている。
  `drain_commands` の除外は正しい（待ちではなく排出）が、こちらは待ちそのもの。
  ホーミング + カメラ Z 移動の完了後に入るので、まさに離席しうる場面。
  ボタン操作待ちである `run_loading_loop` を通知した判断とも不整合。
- 修正: `while True:`（posctrl.py:182）の直前に 1 行。ポーリングループなので
  ループ内ではなくループ入口に置く。
- 確信度: 高

## should-fix

### 2. `notify_operator()` が abort 済みでも鳴る

- 対象: `src/web/api/jobs/manager.py:343-345`
- 問題: `_JobRuntime.prompt` と `next_command` は先頭で `self.checkpoint()` するが、
  `notify_operator` はしない。abort 要求後にローディングループ入口へ到達すると、
  作業者を呼び戻す音を鳴らしてから直後の `next_command` が `JobAborted` を送出し、
  ABORTED（無音方針）で終了する。呼ばれて来た作業者の前でジョブは既に死んでいる。
- 再現: ホーミング中に abort → ホーミング完了 → `run_loading_loop` 入口の
  `ctx.notify_operator()` で prompt 音 → `next_command` で JobAborted。
- 修正案: `_JobRuntime.notify_operator` の先頭に `self.checkpoint()`。ただし
  `JobContext.notify_operator` が `JobAborted` を送出しうる契約変更になる点は要判断
  （呼び出し位置はいずれも直後にブロッキング待ちがあるので実害は無い）。
- 確信度: 高（挙動）／中（対処の是非）

### 3. テストギャップは埋められる（前提が誤り）

- 対象: 計画書「残ギャップ」節、`tests/web/api/jobs/test_pasting.py`
- 問題: 「`Klipper` / `PasteApplicator` は自前 ABC ではなく具象クラスのため
  testing-strategy 上 fake を作れない」という前提が事実に反する。
  `tests/helpers.py:587` に `class FakeKlipper(Klipper)` が既にあり、docstring も
  「送信 G-code を記録し、缶詰 config / status を返す**自前 HAL の fake**」
  「実 `XYZStage` / `PasteDispenser` / `ProbeExecutor` をそのまま組み合わせて結合検証できる」
  と明言している。`tests/pcbasm/pasting/test_applicator.py:97` は
  `build_applicator(FakeKlipper(), XYZStage(klipper.readonly), config)` で実
  `PasteApplicator` を組んでおり、`tests/web/api/jobs/test_pasting.py:1157` も
  既に `XYZStage(klipper.readonly)` を使っている。
- 現実的な手: `run_loading_loop` の finish 経路は
  `progress` → `drain_commands` → `log` → `notify_operator` → `next_command` →
  `Finish` → return で、klipper / stage / applicator に一切触れない。よって
  合成ジョブから `run_loading_loop(ctx, FakeKlipper(), XYZStage(fake.readonly),
  build_applicator(...))` を呼び、`{"type": "finish"}` を submit して
  `player.played == (("prompt", config),)` を見る非実機テストが書ける。
  これで loading / paste_solder / toolhead_offset / paste_volume_calibration /
  dispense_calibration の 5 ジョブが共有する入口が固定できる。
- `_calibration_menu_loop` は private なので直接テストは refactor-conventions 違反。
  入口の形が `run_loading_loop` と同型なので、上記を押さえれば残リスクは小さい。
- 確信度: 高

### 4. `orthogonality_test` が巡回 1 点ごとに鳴る

- 対象: `src/web/api/jobs/posctrl.py:602`（`confirm_next_point` が点ループ内、周回ごとに再走）
- 問題: 「鳴りすぎる場面」の最右翼。ベルトテンションを調整しながら装置の前に張り付く
  作業で、点数 × 周回数だけビープが続く。
- 方針 1（prompt 常時）はユーザー決定なので実装は準拠しており、実装の誤りではない。
  ユーザーに事実として提示して可否を確認する価値がある。
- 確信度: 高（挙動）／判断はユーザー

## nit

### 5. 未使用パラメータが残った

`tests/web/api/jobs/conftest.py:92` の `register_gated(notify_on_completion=...)` は
本変更で全呼び出し元から消え、未使用になった（`register_synthetic` 側は
`test_manager.py:1245` で現役）。AGENTS.md「自分の変更で生じた未使用コードだけを片付ける」。
確信度: 高

### 6. `TestPromptPositiveNumberNotification` がほぼ重複

`notify` 廃止後、`tests/web/api/jobs/test_pasting.py:1939` のこのクラスは
`TestAudioOperatorNotification.test_every_prompt_plays_the_input_sound` と同じ契約を見る。
固有の価値は「非正入力の再プロンプトでも鳴る」1 点のみ。確信度: 中

### 7. docstring の折返しが不自然

`tests/web/api/jobs/test_dev.py:164`「prompt 2 回ぶんに続く 3 / 回目が」、
`tests/web/api/jobs/test_pasting.py:1942`「再プロンプト したときも鳴らす（1 / 度目を」。
docformatter の折返し由来で読みにくい。確信度: 高（可読性のみ）

### 8. 入力待ち音だけ machine 設定がジョブ開始時に固定

`manager.py:739` の `_operator_notifier(machine)` はクロージャに `machine` を束縛するが、
`_play_completion_sound` は `context.machine.audio` を毎回参照する。ジョブ実行中に
音量 / デバイスを変えると入力待ち音だけ古い設定で鳴る。既存の作りのままだが、
入力待ち音の出番が大幅に増えたぶん露出が増えた。確信度: 中

### 9. `uses_machine=False` の計算ジョブは失敗も無音

`dataset_finalize` / `paste_volume_refit` は実時間のかかりうる処理だが成功も失敗も鳴らない。
方針 2・3 どおりなので違反ではないが、元要求「他に失敗したとき（手動中止以外）にも鳴るべき」
からはこぼれている。確信度: 中（元要求の解釈）

### 10. 完了音の対象ジョブを固定するテストが無い

`tests/web/api/jobs/test_catalog.py:415` は `notify_on_completion`（ブラウザ通知）の集合を
固定しているが、音の条件が `uses_machine` へ移った今、`uses_machine` 集合のピンがあると
「どのジョブが鳴るか」が生きた仕様になる。確信度: 中

## 問題なしを確認した点

- `PromptSpec.notify` は `prompt_payload` / `models.py` に露出していなかったため、
  削除による API 契約破壊は無い
- ドキュメント同期は完了（README 表、`data/config-templates/README.md`、
  `dev/audio.html`、`hal/audio.py` / `routers/audio.py` の docstring）。
  `/dev/audio` には元から `data-sound="prompt"` のテスト再生ボタンがあり、
  README の「差し替え後はテスト再生で確認」は prompt.wav にも成立する
- 再生失敗はジョブを壊さない（`_play_sound` の try/except + `_warn_sound_failure`）。
  プレイヤー未注入は `_operator_notifier` が no-op ラムダを返す
- サブエージェント Write の成果物汚染（`</content>` 等）は無し

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（3453 passed / 154 deselected, 134s）
