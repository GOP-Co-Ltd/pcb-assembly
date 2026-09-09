# 未完了 paste dataset の救出と質量入力の通知音

## 背景

2026-09-09 の収集（`plate-47.5x20-20260909T093905.873+0900.incomplete`）で、
全 835 枚 × pre/post の撮影を終えたあと WebUI が不調になり質量プロンプトへ
応答できず、metadata.json を生成できないまま `.incomplete` へ落ちた。
画像は無傷だが、セル配置・量割り当て・実行 rotations がすべて
プロセスメモリ上にしか無く、`ctx.log` に出ていた `配置シード` も
API プロセス再起動（10:39）で失われた。

## 計画

### A. 質量が無くても復元できるようにする

1. `pcbasm/pasting/dataset/pending.py`（新規）
   - `PasteDatasetPending`: metadata v2 から「計量質量に依存する値」だけを
     抜いた schema v1 DTO。抜くのは `total` と、sample / purge の
     `measured_volume_ul`。blank は常に 0.0 なので `PasteDatasetBlank` を再利用
   - `parse_pending(dict)` / `finalize_pending(pending, measured_mass_mg)`
2. `writer.PasteDatasetWriter.write_pending(pending)`：作業 session へ
   `pending.json` を書く。`finalize()` は metadata.json 書き込み後に
   pending.json を削除
3. `writer.finalize_incomplete(session, metadata)`（module 関数）：
   `*.incomplete` へ metadata.json を書いて完成名へ rename
   `writer.rescuable_sessions(root)`：pending.json を持つ `*.incomplete` 一覧
4. `recorder.build_pending(run)` / `write_pending(run)`。`finalize()` は
   `build_pending` + `finalize_pending` へ委譲（重複ロジックを作らない）
5. 収集ジョブ：post 撮影完了直後、**質量プロンプトの前**に `write_pending`
6. 新ジョブ `paste_dataset_finalize`（pasting タブ、装置不要）：
   param `measured_mass` [mg] + 実行時 choice プロンプトで session を選び、
   metadata.json を書いて完成名へ rename・zip 化

### B. 質量入力の通知音

- `PromptSpec.notify: bool`（既定 False）を追加。`_JobRuntime.prompt` が
  応答待ちに入ったとき機体スピーカーで `prompt` 音を鳴らす
- `hal/audio.py` の `Sound` に `"prompt"` を追加、`sounds/prompt.wav`
  （success/failure と同じ 16-bit PCM WAV。ユーザー提供）
- 収集ジョブの質量プロンプトだけ `notify=True`。開始直後の confirm 2 件は
  作業者が装置前に居るので鳴らさない
- `/api/audio/test` と dev/audio.html にテスト再生を追加

## 採否

- 却下: 収集ジョブが `metadata.json` を仮質量で書いて後から上書きする案。
  仮の教師値がディスク上に存在する時間帯ができるため
- 却下: 救出を `scripts/` の CLI にする案。AGENTS.md「開発・運用の操作は
  WebUI のジョブとして提供する」に反する
- 採用: pending.json は metadata から質量依存フィールドだけを抜いた形。
  救出時の合成が「配分して差し込む」だけで済み、復元経路が metadata と
  同じ DTO / converter を共有する

## 今回分の救出（別作業）

blank は pre/post 差分で一意に判定できた（sample 30 / 33 / 80 のみ差分 0、
次点は 238 px）。capacity == target_count == 167 なのでセル配置はシード非依存、
シードは量割り当て（`rng.shuffle`）にしか効かない。CPython の MT19937 を C で
再現して 1..2^31-1 を総当たりし、blank 位置一致でふるったうえで
blob 面積と割り当て量の相関で一意化する。

## 自己レビュー（段階 4）

`git diff --cached` を通しで読んで拾った 2 点。どちらも対応済み。

1. **`.tmp` が救出対象から漏れていた。** 当初 `rescuable_sessions` は
   `*.incomplete` だけを見ていた。これはジョブが例外・中止を捕まえて畳んだ形で、
   プロセスごと落ちると作業 directory は `.{stem}.tmp` のまま残る。今回の事故は
   前者だったが、「復元可能にする」の趣旨からは後者も塞ぐべきなので両方を対象にした。
   実行中の session を掴む恐れは無い（収集ジョブが装置ロックを持つ間は救出ジョブが
   start できない）
2. **連番採番の重複。** `_available_stem`（作業中・未確定・完成の 3 つと衝突回避）と
   救出側の採番が同じループを二度書いていた。`_first_free_name(base, taken)` に
   まとめ、衝突条件だけを差し替える形にした

## 申し送り（実機確認）

- 収集ジョブの計量プロンプトで実際にスピーカーが鳴ること。音源は
  `src/pcbasm/hal/sounds/prompt.wav`（ユーザー提供）。WebUI の「通知音」ページに
  「入力待ち音を再生」ボタンがある
- 収集ジョブを 1 本通し、`pending.json` が計量プロンプトの前に落ちること
- `paste_dataset_finalize` で実データを確定できること
