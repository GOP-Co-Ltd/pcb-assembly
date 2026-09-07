# コア ML 基盤 MR4: training core と experiment tracking（spec-test-author）

正典: `memory/agents/implementation-planner/ml-core-4-training-experiment.md` の
「公開インターフェース案」節と「テスト観点」節、および
`memory/agents/orchestrator/ml-core-4-training-experiment.md` の裁定 3 件。

## 書いたファイル一覧

| ファイル | 役割 |
| --- | --- |
| `tests/ml/support.py` | 合成回帰タスク一式（グループ 0） |
| `tests/ml/experiment/__init__.py` | package docstring |
| `tests/ml/training/__init__.py` | package docstring |
| `tests/ml/experiment/test_logger.py` | グループ A |
| `tests/ml/experiment/test_provenance.py` | グループ A |
| `tests/ml/experiment/test_mlflow.py` | グループ A（実 local server） |
| `tests/ml/training/test_random_state.py` | グループ B |
| `tests/ml/training/test_task.py` | グループ B |
| `tests/ml/training/test_data.py` | グループ B |
| `tests/ml/training/test_transaction.py` | グループ C |
| `tests/ml/training/test_checkpoint.py` | グループ C |
| `tests/ml/training/test_loop.py` | グループ D |
| `tests/ml/test_architecture.py` | グループ D（実装ステップ 11 の module 追加のみ） |

`tests/ml/model/` 以下と `src/` は一切触っていない。

## `tests/ml/support.py` が固定した前提

`plan-implementer` はこの前提の上でテストが緑になるように実装すること。

- 画像は 8x8・3ch、sample 12 件（train 9 / validation 3 / test 0）
- `BATCH_STRIDE = 8`、`MAX_BATCH_SIZE = 3` なので **1 epoch は train 3 batch**
- encoder / head は計画書の tiny config そのまま（`ENCODER_CONFIG` / `HEAD_CONFIG`）
- `SyntheticTaskOptions.non_finite_at_step` は **`training_step` の呼び出し回数（0 起点）**を指す
    （`global_step` ではない。計画書の「指定 step」を最も観測しやすい形に確定させた）
- `SyntheticRegressionData.sample_ids_for(split)` / `target_for(sample_id)` /
    `options` を追加の public メソッドとして持たせた（テストから期待値を組み立てるため。
    `TrainingData` ABC の要求ではない）
- `RecordingExperimentLogger` は `configured_run_id` / `params` / `metrics` /
    `tags` / `artifacts` / `status` / `end_call_count` / `start_call_count` /
    `flush_call_count` を公開属性として持ち、`metrics_for(key)` で
    `(step, 値)` の並びを取れる。`start()` 前の `run_id` は `RuntimeError`

## 仕様根拠の対応表（計画書「テスト観点」→ テスト）

### 正常系

| 計画書の観点 | テスト |
| --- | --- |
| `RecordingExperimentLogger` が ABC を満たす / 抽象未実装検出 | `test_logger.py::TestExperimentLogger` |
| `TaggedExperimentLogger` の固定タグと素通し | `test_logger.py::TestTaggedExperimentLogger` |
| `GitProvenance.capture` の commit / branch / dirty / fingerprint | `test_provenance.py::TestGitProvenanceCapture` |
| `as_tags()` に生 diff と untracked 本文が出ない | `test_provenance.py::TestGitProvenanceTags` |
| `DependencyVersions.collect()` と `not-installed` | `test_provenance.py::TestDependencyVersions` |
| `sanitize_persisted_uri` / `sanitize_persisted_text` | `test_provenance.py::TestSanitizePersistedUri` / `TestSanitizePersistedText` |
| MLflow 実 server への記録と読み戻し / resume | `test_mlflow.py::TestMLflowExperimentLoggerLifecycle` |
| `StepResult.validate()` の正常 | `test_task.py::TestStepResult::test_zero_dimensional_loss_with_positive_samples_is_valid` |
| `GaussianRegressionTask` の loss / reduce キー / 勾配なし評価 | `test_task.py::TestGaussianRegressionTaskTraining` / `TestGaussianRegressionTaskEvaluation` |
| `TrainingData` の計画の決定性と全 sample 1 回 | `test_data.py::TestPlanEpoch` |
| `execute_optimizer_group` の commit 経路 | `test_transaction.py::TestOptimizerGroupCommit` |
| `execute_evaluation_batches` の全件評価 | `test_transaction.py::TestEvaluationBatches::test_all_batches_are_evaluated` |
| `RandomState` の capture / restore 往復 | `test_random_state.py::TestRandomStateRoundTrip` |
| `TrainingProgress` / `BestSelection` の純遷移 | `test_checkpoint.py::TestTrainingProgress` / `TestBestSelection` |
| `CheckpointStore` の往復とファイル名 | `test_checkpoint.py::TestCheckpointStore` |
| Trainer 通し 2 epoch / `end` 1 回 / step 軸 | `test_loop.py::TestTrainerFullRun` |
| 中断あり / なしの weight 一致 | `test_loop.py::TestTrainerInterruptionParity::test_resume_reaches_the_uninterrupted_weights` |
| 未処理 batch からの再開（二重処理なし） | 同上 + `test_signal_stops_at_a_group_boundary` |
| deadline 到達時の正常終了 checkpoint | `test_loop.py::TestTrainerDeadline` |
| SIGTERM が optimizer step 境界で止まる | `test_loop.py::TestTrainerInterruptionParity::test_signal_stops_at_a_group_boundary` |
| compiled wrapper の `_orig_mod.` を残さない | `test_loop.py::TestTrainerCompileSeam` + `test_task.py::TestCompileSeam` |
| `state_dict` キー契約のピン | `test_checkpoint.py::TestModelStateDictContract::test_tiny_model_keys_are_pinned` |

### 異常系

| 計画書の観点 | テスト |
| --- | --- |
| `MLflowRunTarget.validate()` | `test_mlflow.py::TestMLflowRunTarget` |
| MLflow の状態違反（start 前 / 二重 start / end 後 / 欠損 artifact） | `test_mlflow.py::TestMLflowExperimentLoggerRejections` |
| git repo でないディレクトリ | `test_provenance.py::TestGitProvenanceCapture::test_directory_outside_a_repository_is_rejected` |
| `StepResult.validate()` 違反と Trainer 経由の `ValueError` | `test_task.py::TestStepResult` + `test_transaction.py::TestOptimizerGroupRejections::test_invalid_step_result_is_rejected` |
| `GaussianBatch.validate()` | `test_task.py::TestGaussianBatch` |
| `TrainerConfig.validate()` 6 項目 | `test_loop.py::TestTrainerConfig::test_invalid_values_are_rejected`（parametrize） |
| `Trainer.__init__` の `ValueError` | `test_loop.py::TestTrainerConfig::test_trainer_rejects_an_invalid_config` |
| dataset / config / run_id / emergency / model_state キー / batch_plan の resume 拒否 | `test_loop.py::TestTrainerResumeRejections` + `test_checkpoint.py::TestResumeRejection` |
| 非有限 loss で emergency を残して失敗 | `test_loop.py::TestTrainerNonFiniteLoss` |
| `execute_optimizer_group` の 4 拒否経路 | `test_transaction.py::TestOptimizerGroupAbort` / `TestOptimizerGroupRejections` |
| `execute_evaluation_batches` の打ち切り | `test_transaction.py::TestEvaluationBatches` |
| `TrainingCheckpoint.from_payload` の 4 拒否 | `test_checkpoint.py::TestTrainingCheckpointPayload` |
| `CheckpointStore.load` の欠損 / 破損 | `test_checkpoint.py::TestCheckpointStore` |
| `TrainingCheckpoint.validate()` の `_orig_mod.` | `test_checkpoint.py::TestTrainingCheckpointValidation` |
| `RandomState.restore()` の CUDA 拒否 | `test_random_state.py::TestRandomStateRejections::test_cuda_state_cannot_be_restored_on_a_cpu_only_host` |
| monitor 不在の `ValueError` | `test_loop.py::TestTrainerMonitor` |

### エッジケース

| 計画書の観点 | テスト |
| --- | --- |
| accumulation が batch 数を割り切らない | `test_loop.py::TestTrainerEdgeCases::test_accumulation_that_does_not_divide_the_epoch` |
| batch 1 個だけの epoch | `test_loop.py::TestTrainerEdgeCases::test_single_batch_epoch` |
| `max_steps` が 1 epoch 未満で到達 | `test_loop.py::TestTrainerEdgeCases::test_max_steps_stops_inside_the_first_epoch` |
| `early_stopping_patience=0` | `test_loop.py::TestTrainerEdgeCases::test_zero_patience_stops_after_the_first_epoch` |
| 全 epoch で改善しない | `test_loop.py::TestTrainerEdgeCases::test_never_improving_run_keeps_the_first_epoch_as_best` |
| validation observation 0 件 → monitor 不在 | `test_loop.py::TestTrainerMonitor::test_empty_validation_is_rejected` |
| deadline が最初の step 前に到達 | `test_loop.py::TestTrainerDeadline::test_immediate_deadline_finishes_without_a_best_checkpoint` |
| `deadline_seconds=None` | `test_loop.py::TestTrainerDeadline::test_no_deadline_runs_to_completion` |
| `TerminationSignals` を非 main thread で使う | `test_loop.py::TestTerminationSignals::test_non_main_thread_does_not_install_handlers` |
| readback validator 失敗時に既存 `latest.pt` が壊れない | `test_checkpoint.py::TestCheckpointStore::test_failed_readback_leaves_the_previous_file_intact` |

## 期待される失敗（解消済み）

初回記述時は `src/ml/experiment/` と `src/ml/training/` が未実装で collection error だった。
`plan-implementer` の実装投入後、`tests/ml` は **563 passed**（skip 0）で全て緑になった。

## 計画書の解釈で判断した点（`plan-implementer` と要調整）

1. **`gradient_overflow` / `non_finite` の切り分け条件。**
    「AMP 有効なら overflow、無効なら `non_finite`」の判定材料が
    `gradient_scaler.is_enabled()` なのか `autocast_enabled` 引数なのかが計画書から決まらない。
    テストは **両方を同時に立てる / 同時に落とす**呼び出しにしてあるので、どちらを見る実装でも通る。
    テストは `_InfiniteGradientTask`（`sqrt` の 0 微分で loss 有限・勾配無限）で
    両経路を決定論的に作っている。CPU `torch.GradScaler("cpu", enabled=True)` が
    step skip を行うことが前提。ここが成立しない場合は連絡してほしい

2. **`start(run_kind=...)` を MLflow のどのタグ名に載せるか**は計画書に無い。
    `test_mlflow.py` はタグ名を固定せず「`run_kind` の値がタグのどれかに現れる」ことだけを検証している

3. **`DependencyVersions.collect()` の python / platform / cuda / cudnn のキー名**が計画書に無い。
    テストは `versions["python"]` が非空であることと、`TRACKED_PACKAGE_NAMES` の各エントリが
    `importlib.metadata` と一致することだけを固定した。`as_params()` は
    `dependency.<name>` へ全エントリを平坦化する前提

4. **`CheckpointStore.save` の readback 検証が失敗したときの例外型**が計画書に無い。
    テストは `ValueError` を期待している（`validate()` の理由文字列を載せる想定）。
    別の型にするなら連絡してほしい

5. **`TrainerConfig` の `log_params` キー名。**
    `test_loop.py::test_configuration_is_logged_as_params` は
    `max_epochs` / `monitor`（attrs フィールド名そのまま）と、
    追加の `config_fingerprint` / `dataset_fingerprint` を期待している

6. **中断 → resume の parity テストは deadline ではなく SIGTERM で作った。**
    計画書は「1 epoch 目の途中で deadline に当てて停止」と書いているが、
    deadline は wall clock 依存で「途中で止まる」ことを決定論的に再現できず、
    さらに resume 時も同じ config（= 同じ `deadline_seconds`）が要求されるため
    2 回目も同じ位置で止まりうる。`_SignallingTask` が
    `training_step` 3 回目の中で `SIGTERM` を送る形にすると、停止位置が完全に決定論的になる。
    deadline 側は「正常終了 checkpoint」の契約のみ別テストで検証している

7. **`state_dict` キー契約ピンに `@pytest.mark.api_contract` は付けていない。**
    `pyproject.toml` の `markers` に `api_contract` が未登録で、`--strict-markers` により
    エラーになるため。`pyproject.toml` は私の編集範囲外なので、
    マーカーを使うなら orchestrator 側で登録の可否を判断してほしい

8. **`test_loop.py` の weight 一致は `atol=0` / `rtol=0`** で書いた。
    落ちた場合は「なぜ緩めるか」をコメントに残して最小限だけ緩める（閾値を実測値へ張り付けない）。
    ただし緩める判断はテスト側の責任なので、`plan-implementer` は勝手に緩めず連絡してほしい

## 実装側に求めること

- `SyntheticRegressionTask` は `GaussianRegressionTask.__init__(model)` と
    `super().training_step(batch)` を呼ぶので、`GaussianRegressionTask` は
    サブクラス化して `training_step` を `@override` できる形にすること
- `TrainingProgress.with_committed_group(next_batch_index)` は
    `global_step += 1` と `next_batch_index = 引数` の両方を行う（テストがそう固定している）
- `TrainingCheckpoint.resume_rejection` の理由文字列には、判定に使った語
    （`emergency` / `dataset` / `config` / `run_id` / 不一致キー名）を含めること。substring で検証している
- `Trainer.run()` の `logger.log_metrics` は epoch ごとに **1 回だけ**、`step=global_step` で呼ぶ。
    キーに `validation/<monitor>` / `learning_rate` / `epoch_seconds` /
    `train_samples_per_second` を含める
- 失敗時（`NonFiniteLossError` / run_id 不一致）は `logger.end` を **1 回だけ** `"FAILED"` で呼び、
    failure タグを `set_tags` で残す（テストは `logger.tags != {}` で観測している）

## tests/helpers.py への追加

なし。MLflow server の起動 probe は
`tests/ml/experiment/test_mlflow.py` に閉じた session fixture にした
（裁定 2 のとおり mlflow の import 可否では skip せず、
server が `/health` に応答しない・起動直後に落ちた場合だけ理由つきで skip する）。

3rd-party のモックは使っていない。fake は自前 ABC の
`RecordingExperimentLogger`（`ExperimentLogger` 実装）のみ。

## 検証結果

- `make format`: pass
- `uv run pytest tests/ml -m "not hardware"`: collection error（`ml.training.*` / `ml.experiment.*` が未実装）。仕様 first として期待どおり
- `make type` は `src/` 未実装のため未実行


## 2 巡目（orchestrator 指示による修正、2026-09-04）

1. `test_data.py::TestMaterialize::test_target_is_derived_from_the_sample_identity` の
    `pytest.approx()` に入れ子 list を渡していた `TypeError` を修正。
    `batch.target.reshape(-1).tolist()` と 1 次元の期待値を比較する形にした
2. pyright 25 error を解消
    - `test_transaction.py` の `_parameters_snapshot` / `_parameters_changed` / `_run_group` を
        PEP 695 の generic 関数（`[BatchT, ObservationT]`）にした。
        `TrainingTask[object, object]` へ代入できないのは `reduce(observations: Sequence[ObservationT])`
        により `ObservationT` が不変になるためで、`src/` 側の契約は正しい。
        `can_commit` も `Callable[[], bool] | None` へ型付けし、`# pyright: ignore` を除去した
    - 抽象メソッド未実装の検出テスト 3 本（`_IncompleteLogger` / `_IncompleteData` /
        `_IncompleteTask`）に、その行だけ `# pyright: ignore[reportAbstractUsage]` を付けた
3. `TrainerConfig.fingerprint` の対象フィールドを pin するテストを追加
    （`test_loop.py::TestTrainerConfig`）
    - module 定数 `FINGERPRINT_FIELDS`（意味論 17 件）と
        `FINGERPRINT_EXCLUDED_FIELDS`（時間予算 4 件）を literal で固定し、
        `test_every_field_is_classified_as_semantic_or_time_budget` が
        「両者が排他かつ和が `attrs.fields(TrainerConfig)` の全名と一致」を検証する。
        新しいフィールドを足した人は必ずどちらかへ分類させられる
    - 除外の基準（その run 限りの時間予算であって学習の意味論を変えないもの。
        含めると deadline で中断した run を新しい予算で resume できなくなる）を
        `FINGERPRINT_EXCLUDED_FIELDS` の docstring 相当のコメントに明記した
    - 併せて、除外 4 件が fingerprint を変えないこと・意味論 17 件がそれぞれ
        fingerprint を変えることを parametrize で検証している

### 2 巡目の検証結果

- `make format`: pass（2 回とも無変更）
- `make type`: 0 errors, 0 warnings
- `uv run pytest tests/ml -m "not hardware"`: **563 passed**（49.55s）。
    MLflow の実 local server テストも skip されず実行された

## 3 巡目（2 巡目レビューの「修正はされたがテストが無い」分の回帰テスト）

編集範囲は `tests/ml/` のみ。`src/` は変異実験で一時的に書き換えたが、
すべて復元済みで `git diff -- src/` は空。

### 追加・変更したテスト

`tests/ml/support.py`

- `RecordingExperimentLogger.log_params` に MLflow の「param は不変」契約を持たせた。
    既存 key を異なる値で再送したら `RuntimeError`、同値の再送は無害。
    must-fix 1 の根本原因（fake が契約を模していない）をここで塞ぐ

`tests/ml/training/test_loop.py`

- `TestTrainerDeviceSeam::test_materialize_receives_the_trainer_device`：
    `Trainer(device=torch.device("cpu", 0))` の device が train / validation 両方の
    `materialize` へ届くことを、device を記録する `_RecordingDeviceData` で検証。
    判別子に index 付き CPU device を使うので、`torch.device("cpu")` 固定と区別できる
- `TestTrainerForeignBestCheckpoint::test_final_checkpoint_ignores_another_runs_best`：
    同じ checkpoint directory で run A（best.pt を残す）→ run B（`max_steps=1` なので
    自分の best を書かない）を回し、B の `final.pt` が A の重みでないこと・
    `outcome.best_checkpoint_path is None`・`finalization.best_checkpoint_ignored` tag を検証
- `TestTrainerLoggerFailures::test_logger_failure_ends_the_run_as_failed`：
    指定メソッドだけ投げる `_FailingExperimentLogger`（自前 ABC の double。モックではない）を
    `log_params` / `set_tags` で parametrize し、`status == "FAILED"` /
    `end_call_count == 1` / 元例外の型が表に出ることを検証
- `TestTrainerGradientOverflow`：連続 overflow がちょうど 8 回で `NonFiniteLossError`、
    7 回なら回復して `max_epochs` まで走ることを検証。
    有限 loss のまま非有限勾配を作る `_NonFiniteGradientTask`（sqrt の 0 における微分）を
    AMP 有効で回す
- `TestTrainerResumeRejections::test_rejected_resume_leaves_the_caller_model_untouched`：
    run_id 不一致で `ValueError` になったあと、呼び出し側 model の重みが resume 前のままである
    ことを検証。checkpoint 側の重みが初期重みと異なることも assert して、検証が空回りしないようにした
- `TestTrainerResumeWithANewTimeBudget::test_resume_with_a_different_deadline_is_recorded`：
    同じ logger へ別の `deadline_seconds` で resume しても記録が破綻しないことを検証。
    fake の param 不変契約と組み合わさり、時間予算が param 側へ戻ると実際に落ちる
- `TestTrainerFullRun::test_every_semantic_field_is_logged_as_a_param`：
    平坦化後の `compile_options.*` キーも param に載ることを追加で検証

`tests/ml/experiment/test_provenance.py`

- `TestSanitizePersistedUri::test_uri_is_sanitized_as_pinned`：指定の 12 ケースを parametrize で pin。
    credential 除去（`@` を含む形）と、authority を持たない URI が壊れないこと
    （`sqlite:///` の潰れが 2 巡目の回帰）の両方を守る
- `TestSanitizePersistedText::test_credentials_without_a_host_are_removed_in_text`：
    自由文経由でも host 無し URI の userinfo が消えること
- `TestUntrackedContentSizeLimit`：上限超過ファイルは本文が省略され見出しに `size` と `sha256` が載ること、
    小さいファイルの本文は残ること、本文を落としても `diff_fingerprint` が内容変化に追従すること

### 変異実験（テストごとに、機構を潰すと落ちることを実測）

いずれも `-p no:randomly --tb=no` で対象ファイルを実行。変異は毎回バックアップから復元した。

| 変異 | 結果 | 落ちたテスト |
| --- | --- | --- |
| `device=self._device` → `torch.device("cpu")` 固定 | 1 failed / 73 passed | `TestTrainerDeviceSeam::test_materialize_receives_the_trainer_device` |
| `sanitize_persisted_uri` を 2 巡目前の `urlunsplit` 版へ | 5 failed / 23 passed | `test_uri_is_sanitized_as_pinned` の 4 ケース（`sqlite:///mlruns.db` / `http://user:secret@` / `http://user:secret@/path` / `databricks://a:b@`）と `test_credentials_without_a_host_are_removed_in_text` |
| `_best_of_this_run` の run_id 照合を削除 | 1 failed / 73 passed | `TestTrainerForeignBestCheckpoint::test_final_checkpoint_ignores_another_runs_best` |
| untracked のサイズ上限を無効化（1024**5） | 1 failed / 27 passed | `TestUntrackedContentSizeLimit::test_oversized_file_body_is_omitted` |
| `_capture_failure` の `set_tags` 握りを外す | 1 failed / 73 passed | `TestTrainerLoggerFailures::...[set_tags]`（`end_call_count` が 0 になる） |
| overflow 閾値 `>=` → `>` | 1 failed / 73 passed | `TestTrainerGradientOverflow::test_eight_consecutive_overflows_fail_the_run` |
| `as_params` の除外を外す（時間予算を param へ戻す） | 2 failed / 73 passed | `test_time_budget_fields_are_tags_not_params` と `TestTrainerResumeWithANewTimeBudget::test_resume_with_a_different_deadline_is_recorded` |
| `as_params` の `CompileOptions` 平坦化を無効化 | 1 failed / 73 passed | `TestTrainerFullRun::test_every_semantic_field_is_logged_as_a_param` |
| `_restore` の `model.load_state_dict` を run_id 照合の前へ戻す | 1 failed / 73 passed | `TestTrainerResumeRejections::test_rejected_resume_leaves_the_caller_model_untouched` |

### 検証結果

- `make type`: 0 errors, 0 warnings（3 種目とも 0）
- `uv run pytest tests/ml -m "not hardware"`: **592 passed**（50 秒。3 巡目前は 571）
- `git diff -- src/`: 空（変異はすべて復元済み）
- `make format`: **codespell だけ Failed**。原因は
    `memory/agents/code-reviewer/ml-core-4-training-experiment.md:457`（pyright の出力を
    そのまま引用した行の綴り）で、commit `953ed13` に既にあるもの。
    3 巡目の変更とは無関係なので触っていない。ruff / ruff-format / docformatter を含む
    他の hook はすべて Passed で、追加したテストファイル単体では codespell も Passed

### 実装側への申し送り

- 3 巡目で `src/` は 1 行も変更していない。上表の変異はすべて orchestrator の src 修正が
    正しく効いていることの裏付けであり、修正要求ではない
- 2 巡目 should-fix 1（`_RunState` 構築を try の内側へ）と should-fix 2（digest の I/O が
    青天井）は、今回テストを足していない。前者は「`store.load` が投げる」状況を作る必要があり、
    後者は docstring での明示に留める裁定だったため
