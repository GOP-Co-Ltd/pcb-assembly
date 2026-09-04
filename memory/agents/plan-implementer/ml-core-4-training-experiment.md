# コア ML 基盤 MR4: training core と experiment tracking（plan-implementer）

正典: `memory/agents/implementation-planner/ml-core-4-training-experiment.md` +
`memory/agents/orchestrator/ml-core-4-training-experiment.md`（裁定 3 件）+
orchestrator からの追加裁定 4 件（`gradient_scaler.is_enabled()` / marker 不使用 /
atol=0 維持 / SIGTERM parity）。

## 書いたファイル（`src/ml/` のみ。`tests/` は一切触っていない）

| ファイル | 層 |
| --- | --- |
| `src/ml/experiment/__init__.py` | - |
| `src/ml/experiment/logger.py` | 依存フリー |
| `src/ml/experiment/provenance.py` | 依存フリー |
| `src/ml/experiment/mlflow.py` | `ml-train`（唯一 `import mlflow`） |
| `src/ml/training/__init__.py` | - |
| `src/ml/training/random_state.py` | `ml-runtime` |
| `src/ml/training/task.py` | `ml-runtime` |
| `src/ml/training/data.py` | `ml-runtime` |
| `src/ml/training/transaction.py` | `ml-runtime` |
| `src/ml/training/checkpoint.py` | `ml-runtime` |
| `src/ml/training/loop.py` | `ml-runtime` |

ABC は計画どおり 3 つ（`ExperimentLogger` / `TrainingTask` / `TrainingData`）。
Protocol は使っていない。全実装メソッドに `@override`。値オブジェクトは全部
`@attrs.frozen`（Tensor を持つものだけ `eq=False`）。

## 計画からの逸脱（要確認）

### 1. `TrainerConfig.fingerprint` から 4 フィールドを除外した（**要裁定**）

計画は `fingerprint_json(attrs.asdict(self))`（全フィールド）だが、実装は
`_FINGERPRINT_EXCLUDED_FIELDS = ("deadline_seconds", "finalization_grace_seconds",
"checkpoint_interval_steps", "checkpoint_interval_seconds")` を除いている。

理由: `deadline_seconds` を fingerprint に含めると、**deadline で中断した run を
新しい時間予算で resume できなくなる**（`resume_rejection` が config 不一致で拒否する）。
deadline 機能そのものが成立しない。残り 3 つも同種の「その run 限りの時間予算」で、
学習の意味論を一切変えないので同じ扱いにした。

影響: 「`TrainerConfig` を 1 フィールド変えて resume → `ValueError`」のテストは
`learning_rate` 等の意味論フィールドで書かれているため通っている（確認済み）。
上記 4 フィールドを変えても resume は拒否されない。

### 2. `resume_rejection` の `run_id` を手順 3 で「checkpoint 自身の値」で呼んでいる

計画の手順 3 は `resume_rejection(...)` で拒否、手順 4 で `logger.start()` 後に
`run_id` を照合、という順序。しかし `resume_rejection` は `run_id` を必須 keyword に
取るので、まだ logger を start していない手順 3 では実 run_id を渡せない。

実装: 手順 3 では `run_id=checkpoint.run_id`（= 恒真）で role / dataset / config /
model_state キーだけを判定し、手順 4 で `run_id != checkpoint.run_id` を直接比較して
`_end_run("FAILED")` → `ValueError`。`Trainer._load_resume_point` にコメントを残した。
シグネチャは計画どおりのまま（テストは直接呼んで run_id 不一致も検証している）。

### 3. `TrainingProgress.to_payload` / `from_payload`、`BestSelection.to_payload` /
`from_payload` を追加した

計画の公開インターフェース案には無いが、`RandomState` と同じ形にそろえた。
「クラスに属する関数を module-level に置かない」規約に従うと、checkpoint payload の
組み立て・検証はこの 2 クラスのメソッドになるため。

### 4. `TrainerConfig.as_params()` を追加した

計画の手順 5「`TrainerConfig` の全フィールドを `log_params`」を Trainer の中に
埋め込まず、config 自身のメソッドにした（同じ規約）。`compile_options` だけは
`compile_options.backend` のように平坦化する（`log_params` は `Scalar` しか受けない）。
テストが要求する `max_epochs` / `monitor` / `config_fingerprint` / `dataset_fingerprint`
は満たしている。

### 5. `Trainer.device` プロパティを追加した

`device` は `__init__` の keyword で受け取り fingerprint に含めない、という計画の
判断を外から確認できるようにするため。

### 6. 非有限 loss の失敗処理を 1 本の except 経路に統合した

計画の手順 9（`non_finite` → `emergency.pt` → failure タグ → `_end_run("FAILED")` →
`NonFiniteLossError`）と手順 12（例外が貫いた場合の同じ処理）は同じ内容なので、
loop 内では `NonFiniteLossError` を送出するだけにして、手順 12 の except が
emergency 保存・failure タグ・`_end_run("FAILED")`・再送出をまとめて行う。
`progress` は commit 時しか進めないので、この時点で既に group 先頭を指している。

### 7. `_end_run` を finalization から外し、run() の末尾で 1 度だけ呼ぶ

計画では手順 10 の finalization が `_end_run("FINISHED")` を呼び、手順 11 で
signal なら `"KILLED"` としているが、`_end_run` は内部 flag で 1 回しか通らないので
その順序だと `"KILLED"` に到達できない。status の決定を 1 箇所に寄せた。

### 8. `_RunState`（`@attrs.define(eq=False)`、module-private）を置いた

`Trainer.run()` を 150 行の 1 メソッドにしないため、run に固定された文脈（optimizer /
scheduler / scaler / run_id / deadline）と可変状態（progress / selection / stop_reason /
checkpoint パス）を 1 個の private な可変 attrs クラスへ集めた。手順 12 の except 節が
失敗時点の状態をそのまま読めるようにするためでもある。値オブジェクトではないので
`frozen` ではない。

### 9. `GitProvenance.capture` の untracked 走査を全体にした

MR185 は `src` / `tests` / `docs` / `pyproject.toml` / `Makefile` / `.gitlab-ci.yml` に
限っていたが、計画に制限の記載がなく、`tests/ml/experiment/test_provenance.py` は
repository 直下の `extra.txt` が `untracked_files` に載ることを要求している。
`git ls-files --others --exclude-standard`（`.gitignore` は尊重）に統一した。

`untracked_content` も MR185 の base64 ではなく生テキストにした（テストが
`SECRET_MARKER in provenance.untracked_content` を要求）。バイナリは
`errors="replace"` で読むが、見出し行に生 byte の `sha256:` を載せるので
`diff_fingerprint` は正確なまま。生本文はどこにも永続化しない（`as_tags()` に出ない）。

## orchestrator 裁定への対応

- **裁定 1（`gradient_scaler.is_enabled()`）**: そのとおり実装済み。`autocast_enabled`
    は `torch.autocast` にだけ渡す。overflow 経路では `scaler.step()` → `scaler.update()`
    を呼び切ってから rewind する（scale を下げないと再試行が同じ結果になるため）
- **裁定 2（marker 不使用）**: `src/` 側の対応なし
- **裁定 3（atol=0 維持）**: 緩めていない。SIGTERM parity テストは通っている
- **裁定 4（SIGTERM parity）**: 実装側の変更なし

## MLflow adapter のエラーメッセージ

`tests/ml/experiment/test_mlflow.py` が `"start"` / `"end"` を substring で見ているため、
`RuntimeError` の文言を `"MLflow run をまだ start していません"` /
`"MLflow run はすでに start しています"` / `"MLflow run はすでに end しています"` /
`"end 済みの MLflow run は start し直せません"` にした。

`resume_rejection` の理由文字列も `emergency` / `dataset` / `config` / `run_id` /
不一致キー名を含む形にそろえた（spec-test-author の要求どおり）。

`mlflow.flush_async_logging()` は mlflow 3.15.2 に存在することを確認済み。
自前の metric キュー・retry・`/health` probe は作っていない。

## 検証結果

- `make format`（`pre-commit run -a`）: 2 回連続で pass（1 回目に docformatter が
    日本語の 1 文を 2 行から 1 行へ畳んだ 2 箇所を、1 文 1 行へ書き直して解消）
- `make type`（pyright）: **`src/` は 0 error**。残る 25 error はすべて `tests/`
    （下記「テスト側に残る問題」）
- `make test-no-hardware` 全体: **3298 passed / 1 failed**（140 deselected、151 秒）。
    唯一の失敗はテスト側のバグ（下記 1）。`tests/pcbasm/` と `tests/web/` は無傷
- `tests/ml/test_architecture.py` は 3 クラスとも pass。
    `ml.training.*` 6 module が mlflow / hydra / optuna / onnx を読まないこと、
    `ml.experiment.logger` / `provenance` が torch すら読まないことを確認済み

## テスト側に残る問題（`plan-implementer` は `tests/` を編集しないので未修正）

1. `tests/ml/training/test_data.py::TestMaterialize::test_target_is_derived_from_the_sample_identity`
    が `TypeError: pytest.approx() does not support nested data structures` で落ちる。
    `pytest.approx` に `[[1.5], [1.4], ...]` のような入れ子 list を渡している。
    実装とは無関係（`batch.target` の値自体は正しい）
2. pyright 25 error（すべて `tests/`）
    - `tests/ml/training/test_transaction.py` の 22 件: helper が
        `TrainingTask[object, object]` を引数型にしているが、`BatchT` は反変・
        `ObservationT` は不変（`reduce(observations: Sequence[ObservationT])` が
        contravariant 位置に現れるため）なので代入できない。helper 自体を
        `def _run_group[BatchT, ObservationT](task: TrainingTask[BatchT, ObservationT], ...)`
        のように generic にすれば解消する。`src/` 側で直せる筋のものではない
        （`reduce` の契約を壊すことになる）
    - `test_logger.py:37` / `test_data.py:39` / `test_task.py:169` の 3 件:
        「抽象メソッド未実装を検出する」テストで意図的に抽象クラスを
        インスタンス化しているため。pyright を通すには
        `cast` か局所的な `# pyright: ignore[reportAbstractUsage]` が要る

## 確認事項（orchestrator へ）

1. **`TrainerConfig.fingerprint` から 4 フィールドを除外した件（上記「逸脱 1」）**。
    計画どおり全フィールドに戻すなら、deadline 中断からの resume は
    「同じ `deadline_seconds` で再開する」しかなくなり、
    `TestTrainerDeadline` 系の契約と両立しなくなる
2. **上記テスト側 2 件の扱い**（`pytest.approx` の入れ子と pyright 25 error）。
    `make type` / `make test-no-hardware` のグリーン化には
    `spec-test-author` 側の修正が必要
