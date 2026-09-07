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
