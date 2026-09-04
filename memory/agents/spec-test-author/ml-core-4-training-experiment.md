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
