# コア ML 基盤 MR4（training core + experiment tracking）レビュー

対象: `feature/2026-09-04/ml-core-4-training-experiment` の 3 commit
（`git diff ccff4f9^^..7f8b90f`）。main 取り込み merge はレビュー対象外。

## verdict: request-changes

must-fix 4 件。うち 2 件は実装の欠陥（MLflow resume・device seam）、
2 件は「テストが守っているつもりで守っていない」類（readback validator・RNG 復元）。
計画からの逸脱 5 件はいずれも判断そのものは妥当だが、逸脱 1（fingerprint 除外）に
production で破綻する副作用がある（must-fix 1）。

## must-fix

### 1. 時間予算フィールドを fingerprint から外したまま `log_params` に載せているので、MLflow 相手の resume が必ず落ちる

- 対象: `src/ml/training/loop.py:63-68`（`_FINGERPRINT_EXCLUDED_FIELDS`）、
    `src/ml/training/loop.py:207-220`（`TrainerConfig.as_params`）、
    `src/ml/training/loop.py:378-384`（`run()` の `log_params`）
- 問題: 逸脱 1 は「deadline で中断した run を新しい時間予算で resume できるようにする」ための
    除外。resume 自体は通る（実測）が、直後の `log_params` が同じ run へ
    `deadline_seconds=7200.0`（前回 `3600.0`）を再送する。MLflow は param の値変更を拒否する。
- 根拠（実測、mlflow 3.15.2 / sqlite backend）:
    `mlflow.log_params({'deadline_seconds': '3600.0'})` → `end_run` →
    `start_run(run_id=...)` → `log_params({'deadline_seconds': '3600.0' 以外})` で
    `MlflowException: Changing param values is not allowed. Params were already logged=...`。
    同値の再送はエラーにならないので、時間予算を変えたときだけ落ちる。
    Trainer 側の再現: `deadline_seconds=3600` で SIGTERM 中断 → `deadline_seconds=7200` で
    `resume_from=latest` すると `logger.params["deadline_seconds"]` が `7200.0` に変わる
    （`RecordingExperimentLogger` なので現状は素通り）。
- 併発する問題: この `log_params` は `try:` にも `with TerminationSignals()` にも入っていないので、
    ここで例外が出ると `_end_run` が呼ばれず MLflow run が RUNNING のまま残る。
- 直す方向（選択は orchestrator 裁定）: 除外 4 フィールドを `as_params` からも外して
    tag か metric へ回す / resume 時は `log_params` を送らない / 値が変わりうる param を
    `set_tags` に寄せる。いずれにせよ `start()` 後の記録は例外経路の内側へ入れる。
- 確信度: 高（MLflow の挙動・Trainer の挙動とも実測）

### 2. device 既定が cuda になりうるのに、batch を device へ移す seam がない

- 対象: `src/ml/training/loop.py:298`・`726-727`（`_default_device()` は cuda があれば cuda）、
    `src/ml/training/loop.py:617-633`（`_training_batches` / `_validation_batches`）、
    `src/ml/training/data.py:238-247`（`materialize` に device 引数がない）
- 問題: `model.to(self._device)` は行うが（`loop.py:325`）、batch は `TrainingData.materialize` が
    返したものをそのまま `task.training_step` へ渡す。`src/ml/training/` 全体で `.to(` は
    `loop.py:325` の 1 箇所のみ。CUDA のあるホストで `device=` を省略すると
    model は cuda・batch は cpu になり、最初の forward で
    "Expected all tensors to be on the same device" で落ちる。
- 根拠: `grep -rn "\.to(" src/ml/training/` の結果が `loop.py:325` の 1 件だけ。
    テストは全て `device=torch.device("cpu")` を明示している（`tests/ml/training/test_loop.py:43`,
    `176-186`）ので、この経路は 1 度も踏まれない。
- 直す方向: `TrainingData.materialize` に device を渡す（ABC を変える）か、
    `Trainer` が batch を移す seam（`TrainingTask.move_batch(batch, device)` 等）を置くか、
    MR4 では `device` を CPU に限定して docstring と `validate` で明示するか。
    MR5 送りにするなら「GPU 実行は未対応」を計画書へ明記すること。
- 確信度: 高（機構）／中（MR4 で直すか MR5 送りかは裁定事項）

### 3. `test_failed_readback_leaves_the_previous_file_intact` が readback validator を 1 度も通らない

- 対象: `tests/ml/training/test_checkpoint.py:457-473`、`src/ml/training/checkpoint.py:456-468`
- 問題: 壊れた checkpoint を `_orig_mod.` prefix で作っているが、`CheckpointStore.save` は
    `checkpoint.validate()`（`checkpoint.py:459-460`）で先に `ValueError` を投げる。
    `atomic_write_stream` にも `_validate_readback` にも到達しない。
    計画のエッジケース「`CheckpointStore.save` の書き込み途中で中断しても既存の `latest.pt` が
    壊れない（`atomic_write_stream` の readback validator が理由を投げるケースで検証）」が未検証のまま。
- 根拠: `save()` の 1 行目が `validate()`、その `validate()` は
    `_orig_mod.` を理由文字列で返す（`checkpoint.py:284-292`。
    `TestTrainingCheckpointValidation::test_compiled_prefix_in_model_state_is_rejected` が
    それ自体を pin している）。テストが assert している「既存ファイルが無事」は
    「そもそも書き込みが始まらなかった」という別の理由で成立している。
- 直す方向: `validate()` は通るが readback で落ちる状況を作る
    （payload に未知キーを混ぜる・`to_payload` を差し替える等）。
- 確信度: 高

### 4. RNG 復元が Trainer レベルで一切テストされていない（中断→resume の一致テストが RNG に非感応）

- 対象: `tests/ml/training/test_loop.py:371-439`（`TestTrainerInterruptionParity`）、
    `tests/ml/support.py:123-234`（`SyntheticRegressionData`）
- 問題: 計画の核である「group 先頭の RNG snapshot → rewind」と
    「resume 時の `RandomState.restore()`」が、Trainer のテストで一切効いていない。
- 根拠（実測）:
    - `RandomState.restore` を `lambda self: (True, None)` へ差し替えて
        `pytest tests/ml/training/test_loop.py` → **60 passed**（1 件も落ちない）
    - `loop.seed_everything` を no-op へ差し替えても **60 passed**
    - 原因: 合成 task / data が global RNG を一切消費しない。model に dropout がなく
        （`grep -rn "Dropout" src/ml/model/` が 0 件）、`plan_pixel_budget_batches` と
        `PaddedBatch.pad` はローカルな `random.Random(seed)` を使う
        （`src/ml/data/batch.py:118`・`218`）
- つまり「中断あり/なしの weight 一致」は optimizer / scheduler / batch 位置の復元しか
    守っていない。RNG が壊れても緑のまま。MR3 の「`manual_seed` が後段で上書きされて
    死んでいる」と同種の、機構が空回りしていても気付けない構造。
- 直す方向: 合成 task の `training_step` に global RNG を消費する項
    （`torch.randn_like` のノイズ等）を 1 本入れて、parity テストを RNG 感応にする。
    `RandomState.restore` を潰すと落ちることを確認してから確定させる。
- 確信度: 高（実測）

## should-fix

### 5. `sanitize_persisted_uri` が host 無し URI の credential を落とさない

- 対象: `src/ml/experiment/provenance.py:324-333`
- 問題: `parsed.hostname is None` の分岐が `parsed.netloc` をそのまま `urlunsplit` へ渡すので、
    userinfo が残る。credential 除去がこの関数の唯一の役目なので、抜けは避けたい。
- 根拠（実測）: `sanitize_persisted_uri("http://user:secret@")` → `'http://user:secret@'`、
    `"http://user:secret@/path"` → `'http://user:secret@/path'`、
    `"databricks://a:b@"` → そのまま。host がある形（`postgresql://user:secret@host:5432/db?...`）は
    正しく落ちる。`sanitize_persisted_text` 経由でも同じく残る。
- 直す方向: hostname が None のときは netloc の `@` より前を無条件に捨てる
    （`_sanitize_malformed_uri` と同じ処理へ寄せる）。
- 確信度: 高

### 6. SIGTERM の「group 境界」assertion が既定 `gradient_accumulation=1` で恒真

- 対象: `tests/ml/training/test_loop.py:400`
    （`assert next_index % _config().gradient_accumulation == 0`）
- 問題: `_config()` の `gradient_accumulation` は 1 なので、この行はどんな値でも通る。
    計画のテスト観点「`next_batch_index` が gradient accumulation の倍数に載っている」は
    未検証。
- 補足（こちらは守れている）: 同じテストの `assert 0 < next_index < TRAIN_BATCHES_PER_EPOCH`
    が signal の inner→outer 伝播を実際に守っている。`execute_optimizer_group` へ渡す
    `can_commit` を `None` に潰すと、このテストだけが `assert 3 < 3` で落ちることを実測済み。
- 直す方向: `gradient_accumulation=2` を parametrize に足す。
- 確信度: 高

### 7. `test_gradients_are_cleared_before_the_group` が zero_grad を守っていない

- 対象: `tests/ml/training/test_transaction.py:160-167`
- 問題: 前 group の勾配を残してから group を回し、`result.outcome == "committed"` しか見ていない。
    `optimizer.zero_grad` を消しても outcome は committed のままなので、このテストは落ちない。
- 直す方向: 残留勾配ありと無しでパラメータ更新後の値が一致することを比べる。
- 確信度: 高

### 8. `best.pt` の epoch が best epoch と一致することを読み戻すテストがない

- 対象: `tests/ml/training/test_loop.py:298-368`（`TestTrainerFullRun`）
- 問題: MR185 の「`best.pt` の `epoch` が best epoch + 1」は**実装としては直っている**が、
    `best.pt` の中身を読む assertion が無い。`outcome.best_epoch == 0`
    （`test_never_improving_run_keeps_the_first_epoch_as_best`）が間接的に守っているだけで、
    `best.pt` 側の `progress.epoch` がずれても検出できない。
- 根拠（実測）: `max_epochs=3` / `minimum_delta=1e9` で run すると
    `best.pt` は `progress.epoch=0` / `selection.best_epoch=0` / `epochs_completed=0`、
    `outcome.best_epoch=0`。実装は正しい。
- 直す方向: `store.load("best")` の `progress.epoch` と `selection.best_epoch` が
    `outcome.best_epoch` と一致することを assert する。
- 確信度: 高

### 9. `_finalize` が別 run の `best.pt` を検証せずに読み込む

- 対象: `src/ml/training/loop.py:409-411`・`684-696`
- 問題: `state.best_checkpoint_path` を run 開始時に `store.exists("best")` だけで埋め、
    `_finalize` は `store.load("best")` の結果を `run_id` も fingerprint も見ずに
    `model.load_state_dict` して `final.pt` として書く。checkpoint directory を使い回すと、
    前 run の best weights が今回の `final.pt` として出る。
    `TrainingOutcome.best_checkpoint_path` も、今回 1 度も best を書いていないのに非 None になる。
- 根拠: resume は `run_id` 一致を要求するので、正当な `best.pt` は必ず同じ `run_id` を持つ。
    `_finalize` にその照合が無い。
- 直す方向: `_finalize` で `best.run_id == state.run_id` を確認し、違えば無視して理由を tag に残す。
    `best_checkpoint_path` の初期化も同じ条件に揃える。
- 確信度: 高（機構）／中（運用上どれだけ踏むか）

### 10. `GitProvenance.capture` が repository 全体の untracked ファイルを全部読み込む

- 対象: `src/ml/experiment/provenance.py:244-247`・`382-404`
- 問題: 逸脱 4 で走査範囲をリポジトリ全体へ広げたうえ、`_untracked_content` が
    該当ファイルを全て `read_bytes()` する。`.gitignore` は尊重するが、
    untracked のデータセット・checkpoint・ログが 1 つでもあると run 開始時に
    それを丸ごとメモリへ読む。MR185 が `src` / `tests` / `docs` などに絞っていたのはこの回避。
    生本文を永続化しない方針（`as_tags` に出さない）自体は計画どおりで妥当。
- 直す方向: 1 ファイル / 合計のサイズ上限を置いて超過分は digest だけにする、
    または対象ディレクトリを絞る。
- 確信度: 高（機構）／中（このリポジトリで実際に踏むか）

### 11. `seed_everything` が `torch.use_deterministic_algorithms` をプロセス全体に立てっぱなしにする

- 対象: `src/ml/training/random_state.py:38-49`
- 問題: 復元しないので、`Trainer.run()` か `seed_everything` を 1 度でも呼ぶと、
    以降そのプロセス全体（= pytest session の残り全部）が deterministic モードになる。
    決定的実装を持たない op を使う後続テストが、実行順に依存して落ちうる。
- 直す方向: 呼び出し側が明示的に戻せる形にする（context manager 化）か、
    少なくとも「プロセス全体に効く」ことを docstring に書く。
- 確信度: 中（現状の suite では顕在化していない。3321 passed）

### 12. `_capture_failure` の `set_tags` が失敗すると `_end_run("FAILED")` に到達しない

- 対象: `src/ml/training/loop.py:698-724`
- 問題: emergency 保存の失敗は握って tag に残す設計になっているが、その直後の
    `self._logger.set_tags(tags)` が投げると `run()` の `except` 節の中で新しい例外が上がり、
    `_end_run("FAILED")` も元例外の再送出もされない。run が終了状態を持たないまま残る。
- 直す方向: `_end_run` を `finally` 側へ寄せるか、`set_tags` も同じ握り方にする。
- 確信度: 高（機構）／低（logger が死んでいる状況限定）

### 13. 中断した epoch で validation を走らせないことを直接守るテストがない

- 対象: `src/ml/training/loop.py:501-506`、`tests/ml/training/test_loop.py:383-401`
- 問題: 実装は正しい（中断 epoch は validation も `log_metrics` もしない）が、
    signal テストは `latest.pt` の `next_batch_index` しか見ていない。
    中断 epoch で metrics が 1 件も出ないことを assert していない。
- 直す方向: `logger.metrics == []` を signal テストへ足す。
- 確信度: 高

### 14. fingerprint の意味論フィールド pin が手書き parametrize に依存している

- 対象: `tests/ml/training/test_loop.py:237-289`
- 問題: `test_every_field_is_classified_as_semantic_or_time_budget` は「新フィールドを
    どちらかへ分類させる」ことは強制できるが、`test_semantic_fields_change_the_fingerprint` の
    parametrize は別の手書きリスト。新しい意味論フィールドを `FINGERPRINT_FIELDS` に
    足しただけでは「本当に fingerprint が変わる」検証は付いてこない。
    既存 17 フィールドについては両方向とも守れている（除外側へ移すと落ちる）。
- 直す方向: parametrize を `FINGERPRINT_FIELDS` から導出する（フィールド名 → 変更値の写像を 1 本置く）。
- 確信度: 高

## nit

- `src/ml/training/loop.py:450-456`: `resume_rejection(run_id=checkpoint.run_id)` は恒真。
    コメントで意図は書かれているが、`resume_rejection` の `run_id` 引数が production で死んでいる。
    判定を 1 箇所に寄せる（引数を落として loop 側の直接比較に一本化する）ほうが読み手に優しい。
    現状の 2 段構えでも振る舞いは正しく、`test_run_id_mismatch_is_rejected` が
    `end("FAILED")` 1 回まで含めて守っている。
- `src/ml/training/loop.py:211`: `as_params` が `attrs.fields(TrainerConfig)` を使う（`type(self)` でない）。
- `src/ml/training/loop.py:546-548`: `train_samples_per_second` の分母 `epoch_seconds` に
    validation とチェックポイント書き出しの時間が入る。
- `src/ml/training/loop.py:593`: `> _MAXIMUM_CONSECUTIVE_GRADIENT_OVERFLOWS` なので実際は 9 回まで許す。
- `Trainer` は `logger.flush()` を一度も呼ばない（`end` 側の flush 頼み。MLflow adapter では十分）。
- AMP 経路（Trainer が `gradient_overflow` を rewind して再試行する）に Trainer レベルのテストがない。
    `execute_optimizer_group` 単体では `TestOptimizerGroupAbort` が守っている。
- `_capture_failure` は monitor 不在の `ValueError` のような設定ミスでも `emergency.pt` を残す。
    計画の手順 12 どおりだが、post-mortem 用途としてはノイズになる。

## 計画からの逸脱 5 件の判定

| 逸脱 | 判定 | 備考 |
| --- | --- | --- |
| 1. fingerprint から時間予算 4 フィールド除外 | 妥当。ただし must-fix 1 | 除外の線引きは `test_every_field_is_classified_as_semantic_or_time_budget` + 両方向の parametrize で守られている（should-fix 14 の穴を除く）。`log_params` 側が追随していないのが問題 |
| 2. `resume_rejection` を手順 3 で恒真呼び、run_id は手順 4 で直接比較 | 妥当 | logger を start する前に実 run_id は取れない。振る舞いは計画どおりで、テストも守っている。読みやすさは nit |
| 3. 非有限 loss を手順 12 の except へ統合 | 妥当 | 手順 9 と手順 12 の処理内容が同一。`progress` は commit 時しか進まないので emergency は group 先頭を指す。テストが `latest.progress.global_step == 2` で守っている |
| 4. `_end_run` を finalization から外し `run()` 末尾で 1 度だけ | 妥当 | 実装者の主張どおり、`_end_run` が冪等な以上、計画の順序では `"KILLED"` に到達できない。`test_signal_stops_at_a_group_boundary` が `status == "KILLED"` を守っている |
| 5. `GitProvenance` の untracked 全体走査 + 生テキスト | 生テキストは妥当、走査範囲は should-fix 10 | 生本文は永続化しない（`as_tags` に出ない）ので計画の意図と整合。バイナリの digest を見出し行に載せる作りで `diff_fingerprint` も正確 |

## MR185 の 9 欠陥の修正確認

| MR185 の欠陥 | 実装 | テストが守っているか |
| --- | --- | --- |
| signal flag が inner→outer へ伝播しない | 修正済み（flag 1 個、`can_commit` が読む） | 守っている（`can_commit` を潰すと signal テストが `assert 3 < 3` で落ちることを実測） |
| MLflow の step 軸が epoch / global_step で混在 | 修正済み（`log_metrics` は 1 箇所、`step=global_step`） | 守っている（steps == [3, 6] を pin） |
| 中断 epoch でも full validation | 修正済み（`loop.py:501-506`） | 間接的のみ（should-fix 13） |
| `best.pt` の epoch が +1 | 修正済み（実測で `best.pt` の `progress.epoch == best_epoch == 0`） | 間接的のみ（should-fix 8） |
| finalization で checkpoint を load → 書き換え → 再 save | 廃止済み（`_finalize` は best を model へ load して `final.pt` を新規保存するだけ） | `test_final_checkpoint_holds_the_best_weights` |
| `logger.end()` が 2 回呼ばれうる | 修正済み（`_run_ended` flag） | 複数テストが `end_call_count == 1` を pin |
| `TrainingDeadlineExceeded` 送出 | 廃止済み（`stop_reason="deadline"` + `FINISHED`） | `TestTrainerDeadline` |
| 失敗時に `latest.pt` を上書き | 修正済み（失敗経路は `emergency.pt` のみ） | `latest.progress.global_step == 2` で守っている |
| `OptimizerBoundary` / `committed: bool` / `MetricReducer` / Protocol 2 本 / `_metric_queue` | すべて廃止（Protocol 0 件、ABC 3 本、キューなし） | `test_architecture.py` + 各 ABC のテスト |

`emergency.pt` を resume 対象から外す契約も `test_emergency_checkpoint_is_not_resumable` で守られている。

## 規約チェック

- ABC 3 本（`ExperimentLogger` / `TrainingTask` / `TrainingData`）、Protocol 0 件、
    実装側は全メソッド `@override`。値オブジェクトは `@attrs.frozen`（Tensor 持ちは `eq=False`）: 適合
- 検証は `validate() -> str | None`、例外は境界のみ: 適合
- 略語の綴り切り: 適合（`cfg` / `idx` / `num_` 等の混入なし）
- `src/ml/` から `pcbasm` / `web` を import しない: 適合（`test_architecture.py` が機械検証、
    `ml.experiment.logger` / `provenance` は torch すら読まない）
- atomic I/O は `ml.artifact.atomic.atomic_write_stream` 1 実装: 適合（`atomic_torch_save` を作っていない）
- テストは `class TestXxx` 集約、private の直接テストなし、3rd-party のモックなし
    （MLflow は実 local server、fake は自前 ABC の `RecordingExperimentLogger` のみ）: 適合
- 成果物汚染（`</content>` 等の混入）: なし

## 検証結果

- `make format`: pass（`pre-commit run -a` 全 hook Passed、working tree 変化なし）
- `make type`: pass（pyright 0 errors, 0 warnings）
- `make test-no-hardware`: pass（3321 passed / 140 deselected / skip 0、142 秒）
    - `tests/ml/experiment/test_mlflow.py` は 15 件とも実 local server で PASSED（skip されていない）
- 実機テスト（`make test` / `pytest -m hardware`）は未実行（方針どおり）

---

# 2 巡目レビュー（修正 3 commit）

対象: `git diff a057b1a..HEAD`（`faa0198` src / `68caaa1` test / `953ed13` memo）。
1 巡目の対象と main 取り込み分は再レビューしていない。

## verdict: request-changes

裁定で受理された 13 件は**すべて実装されている**。見送りにした 3 nit と
should-fix 11 の context manager 化が黙って入り込んでいないことも確認した。

差し戻す理由は 2 つ。

1. must-fix 5 の修正が、authority を持たない URI を壊す新しい欠陥を持ち込んだ
2. 1 巡目の中心的指摘（「機構を潰しても緑のまま」）が、今回の修正 4 件でそのまま再発している。
    must-fix 2 / 5 / 9 と should-fix 10 / 12 は**潰しても 571 passed のまま**

## 受理指摘の修正確認

変異はすべて実施後に `git checkout` で戻し、`git diff` が空であることを確認済み。
変異時の実行対象は `tests/ml`（571 tests）または `tests/ml/training`（162 tests）。

| # | 実装 | テストが守っているか（変異実験） |
| --- | --- | --- |
| 1 時間予算 param → tag | 済（`as_tags` 新設、`log_params` / `set_tags` を try 内へ） | **守っている**: `as_tags` → `{}` で 1 failed |
| 2 device seam | 済（`materialize` に `device`、両呼び出しが渡す） | **守っていない**: `device=self._device` → `torch.device("cpu")` 固定で 571 passed |
| 3 readback validator 未到達 | 済（`_UnreadableCheckpoint` で `validate()` は通る payload） | **守っている**: 新規 3 件が readback / 一時ファイル / 事前 reject を分離 |
| 4 RNG 非感応 | 済（`training_step` に `torch.randn_like` ノイズ） | **守っている**: `RandomState.restore` no-op → 6 failed（Trainer 2 件）、`_rewind` no-op → 2 failed、`seed_everything` no-op → 4 failed（`TestTrainerSeeding` 2 件込み） |
| 5 credential 残留 | 済だが**新欠陥**（下記 must-fix 1） | **守っていない**: 旧実装へ戻して 571 passed |
| 6 group 境界 assertion | 済（`gradient_accumulation` を 1 / 2 で parametrize） | 守っている |
| 7 zero_grad | 済（clean / dirty の重み比較） | **守っている**: `transaction.py:92` の `zero_grad` 削除で 3 failed |
| 8 `best.pt` の epoch | 済（`test_best_checkpoint_records_the_best_epoch`） | 守っている |
| 9 別 run の `best.pt` | 済（`_best_of_this_run` / `_existing_best_path`） | **守っていない**: run_id 照合の削除で 571 passed、`_existing_best_path` を `exists()` 版へ戻しても 162 passed |
| 10 untracked サイズ上限 | 済（1 MiB / 8 MiB） | **守っていない**: 上限を無効化して 571 passed |
| 11 deterministic のプロセス汚染 | docstring のみ（裁定どおり。context manager 化されていない） | — |
| 12 `set_tags` 失敗で `_end_run` 未到達 | 済（`try/except Exception: return`） | **守っていない**: 握りを外して 162 passed |
| 13 中断 epoch の metrics | 済（`assert logger.metrics == []`） | 守っている |
| 14 parametrize 導出 | 済（`FIELD_CHANGES` + `test_every_field_has_a_change_case` で全フィールド網羅を強制） | 守っている |
| nit off-by-one | 済（`>=`） | 境界のテストなし（`>` へ戻して 162 passed） |
| nit `attrs.fields(type(self))` | 済 | — |
| nit `resume_rejection(run_id=)` 削除 | 済（loop 側の直接比較に一本化） | 守っている（`test_run_id_mismatch_is_rejected` が `end("FAILED")` 1 回まで pin） |

見送り分（`train_samples_per_second` の分母、`logger.flush()`、設定ミスでの emergency）は
いずれも変更されていない。

## 指定された確認事項への回答

- **`as_tags` / try 内への移動**: 順序（`log_params` → `set_tags` → `compile_forward` →
    初回 `latest` 保存）は壊れていない。例外経路も `except BaseException` の内側に入った
- **`materialize` の device**: ABC 実装は `SyntheticRegressionData` と `_IncompleteData` の 2 本のみで
    追随済み。`_validation_batches` も渡している。ただしテストが無い（下記 should-fix 1）
- **`best_checkpoint_path` の意味**: run 中（`_save(state,"best")`）／開始時（同 run_id の既存 best）／
    finalization（同 run_id のみ採用）で食い違いはない。finalization 猶予切れで `_finalize` を
    飛ばす経路でも、開始時に別 run の best を掴んでいないので矛盾しない
- **`resume_rejection` の `run_id` 削除**: loop 側の照合は効いており、`end("FAILED")` は 1 回
- **`_capture_failure` の握り**: 元例外の再送出と `_end_run("FAILED")` に到達する（コード読みで確認、テストは無い）
- **untracked のサイズ上限と `diff_fingerprint`**: 省略側も
    `size {size}; sha256:{sha256_file(...)}` を見出しに載せるので、本文の有無に関わらず
    内容が変われば fingerprint が変わる性質は保たれている

## must-fix

### 1. `sanitize_persisted_uri` の新分岐が、authority を持たない URI に `//` を挿入して壊す

- 対象: `src/ml/experiment/provenance.py:174-178`
- 問題: `hostname is None` の分岐が無条件に `f"{scheme}://{authority}{path}"` を組む。
    authority を持たない URI（`scheme:path` 形）は元々 `//` を持たないので、出力が別物になる。
- 根拠（実測）:
    - `file:./mlruns` → `file://./mlruns`（`./mlruns` が host 扱いになる形）
    - `mailto:user@example.com` → `mailto://user@example.com`（credential も落ちていない）
    - `urn:uuid:1234` → `urn://uuid:1234`
    - 修正前はいずれも `urlunsplit` 経由で原文どおりだった
    `file:./mlruns` は MLflow の正規の tracking URI 形式で、
    `MLflowRunTarget.sanitized_tracking_uri`（`src/ml/experiment/mlflow.py:55`）が
    この関数を通して params / tags へ載せる。provenance に誤った URI が残る。
- 直す方向: `netloc` が空のときは元の `urlunsplit` 経路に戻し、`//` を組み立てるのは
    `netloc` が非空のときだけにする。credential 除去（`http://user:secret@/path` → `http:///path`）は
    そのまま維持できる。
- 確信度: 高（実測）

### 2. 受理した must-fix 3 件（2 / 5 / 9）が回帰テストなしで着地している

- 対象: `src/ml/training/loop.py:641`・`652`（device 引き回し）、
    `src/ml/experiment/provenance.py:174-178`、
    `src/ml/training/loop.py:709-742`（`_best_of_this_run` / `_existing_best_path`）
- 問題: 1 巡目の verdict の中心は「機構が空回りしていても緑のまま」だった。
    今回入った src 修正のうち上の 3 件は、機構を潰しても 1 件も落ちない。
    次のリファクタで静かに戻せる状態のままマージすることになる。
- 根拠（変異実験、いずれも `tests/ml` 571 tests）:
    - `_training_batches` / `_validation_batches` の `device=self._device` を
        `torch.device("cpu")` 固定へ → **571 passed**
    - `sanitize_persisted_uri` を修正前の `urlunsplit((scheme, netloc, path, "", ""))` へ → **571 passed**
    - `_best_of_this_run` の `best.run_id != run_id` 分岐を削除 → **571 passed**
    - `_existing_best_path` を `exists("best")` 版（修正前）へ → 162 passed（`tests/ml/training`）
- 直す方向:
    - device: 自前 ABC の `TrainingData` を実装する recording double を置き、
        `Trainer(device=...)` が `materialize` へその device を渡すことを assert する
        （CPU only host でも device object の同一性は確かめられる）
    - sanitize: `http://user:secret@/path` と `databricks://a:b@` を含む parametrize を
        `TestSanitizePersistedUri` へ足す。`file:./mlruns` のような authority 無し URI が
        素通りすることも同時に pin する（must-fix 1 の回帰防止を兼ねる）
    - best: 別 run_id の `best.pt` を置いた directory で run し、`final.pt` の重みが
        live model 由来であること・`outcome.best_checkpoint_path is None`・
        `finalization.best_checkpoint_ignored` tag が付くことを assert する
- 確信度: 高（変異実験で実測）

## should-fix

### 1. `_existing_best_path` が `logger.start()` の後・`try:` の外に置かれている

- 対象: `src/ml/training/loop.py:417`（`_RunState(...)` の引数）、同 `741-743`
- 問題: 同じ commit が `log_params` / `set_tags` を try 内へ移した理由
    （`loop.py:422-423` のコメント「start() のあとの記録は必ず例外経路の内側へ置く」）が、
    新設の `_existing_best_path` には適用されていない。ここは `store.load("best")` を通るので、
    `TrainingCheckpoint.from_payload` の `float(...)` / `str(...)` 変換が投げうる
    （`_load_payload` は torch の失敗しか握らない）。落ちると `_end_run` が呼ばれず run が RUNNING で残る。
- 併せて: 開始時と `_finalize` で `best.pt` を 2 回フルロードする。run_id 1 個を見るためだけに
    model 重み全体を読むので、大きな model では無視できない。`resume_from is None` の
    新規 run では結果を必ず捨てるため、その場合は読む必要がない。
- 直す方向: `_RunState` の構築を try の内側へ入れる。加えて best の照会を
    `resume_from is not None` のときだけにする。
- 確信度: 中（例外経路の到達性）／高（二重ロードは機構として確実）

### 2. untracked の上限は「メモリ」だけを有界化し、I/O は青天井のまま

- 対象: `src/ml/experiment/provenance.py:252-262`
- 問題: 上限超過ファイルも `sha256_file` で全 byte を読む。untracked に巨大な dataset が
    1 つあると、run 開始のたびにそれを最後まで読む。裁定の「超過分は digest だけにする」を
    満たしてはいるが、1 巡目の懸念（run 開始時の停止時間）は半分しか解けていない。
- 直す方向: MR4 の範囲で許容するなら「メモリのみ有界」であることを docstring に書く。
    解くなら digest 対象にも byte 上限を置き、超過は `size` と mtime だけにする
    （`diff_fingerprint` は同一サイズの内容変更を取りこぼす、というトレードオフを明記する）。
- 確信度: 高（機構）／低（このリポジトリで実際に踏むか）

### 3. `_capture_failure` の握りと `log_params` の try 内移動を守るテストがない

- 対象: `src/ml/training/loop.py:763-766`、`tests/ml/support.py:257-`
- 問題: `RecordingExperimentLogger` は例外を投げないので、
    「`set_tags` が投げても `_end_run("FAILED")` に到達する」も
    「`log_params` が投げたら FAILED で終わる」も 1 度も実行されない。
    握りを外しても 162 passed。
- 直す方向: 指定したメソッドだけ投げる logger double（自前 ABC なのでモックではない）を 1 本置き、
    `set_tags` 版と `log_params` 版で `status == "FAILED"` / `end_call_count == 1` / 元例外の型を assert する。
- 確信度: 高（変異実験で実測）

### 4. `RecordingExperimentLogger` が MLflow の「param は不変」契約を模していない

- 対象: `tests/ml/support.py:306-309`
- 問題: must-fix 1 の根本原因は「値の変わる param を再送すると MLflow が拒否する」ことだった。
    fake は `self.params.update(params)` なので、将来どのフィールドが param 側へ戻っても
    fake 経由では素通りする。今回追加された 2 件のテストは
    「除外 4 フィールドが param に無い」ことを pin しているが、契約そのものは pin していない。
- 直す方向: `log_params` で「既存 key を異なる値で再送したら `RuntimeError`」にする。
    ABC の docstring（`src/ml/experiment/logger.py:54`「Run 中に変わらない設定値」）と揃う。
- 確信度: 高

### 5. `finalization.best_checkpoint_ignored` の tag 値が truncate されない

- 対象: `src/ml/training/loop.py:716`
- 問題: `reason` には `store.load` の失敗理由（パスと torch のエラー文）が入りうるのに、
    隣の `_capture_failure` が使っている `_FAILURE_MESSAGE_LIMIT` の切り詰めが掛かっていない。
    同じ性質の tag で扱いが非対称。
- 確信度: 高（非対称は確実）／低（長さで実害が出るか）

## nit

- `src/ml/training/loop.py:236`: `as_tags` の key が namespace 無し（`deadline_seconds`）。
    他の tag はすべて `git.` / `failure.` / `finalization.` 前置き。`training.` 等へ寄せると衝突しない
- `src/ml/training/loop.py:608`: off-by-one は `>=` で直ったが、境界を pin するテストが無い
    （`>` へ戻して 162 passed）。`_MAXIMUM_CONSECUTIVE_GRADIENT_OVERFLOWS` の意味は未固定のまま
- `src/ml/training/loop.py:436-438`: `_end_run("FAILED")` 自体が投げると `raise` に到達せず元例外が
    表に出ない（`__context__` には残る）
- `src/ml/training/loop.py:388-394`: run_id 不一致で raise する前に `_restore` が呼び出し側の model を
    書き換え済み。拒否した resume の重みが model に残る
- `tests/ml/training/test_loop.py:391-397`: `test_every_semantic_field_is_logged_as_a_param` が
    `compile_options` を除外しており、平坦化後の `compile_options.*` キーは誰も見ていない
- `src/ml/training/loop.py:766`: `except Exception` なので `BaseException` を投げる logger は素通り

## 検証結果

- `make format`: pass（全 hook Passed、working tree 変化なし）
- `make type`: pass（pyright は error も warning も 0 件）
- `make test-no-hardware`: pass（3329 passed / 140 deselected、149 秒）
- 成果物汚染（`</content>` 等）: 変更 13 ファイルとも無し
- 実機テスト（`make test` / `pytest -m hardware`）: 未実行（方針どおり）
- 変異実験で書き換えた 8 ファイル分はすべて復元済み（`git diff` 空を確認）
