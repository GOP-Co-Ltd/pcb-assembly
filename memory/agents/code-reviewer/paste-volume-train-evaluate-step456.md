# paste_volume train / evaluate step 4 + step 5/6 レビュー

対象: `git diff 87f0c78..4af725b -- src tests`
（`fa5f817` step 4 / `931518f` artifact_location / `f074287` train / `0b9efc2` evaluate /
`1dbbc53` search / `f759215` cli / `4af725b` メモ）。`3cdc7a2`（仕様書）は diff 外だが整合確認に使った。

レビューは repo を書き換えずに行った。`4af725b` を `.review-mutants/base/` へ凍結複製し、
変異は `.review-mutants/mut/<name>/` へ複製してから `PYTHONPATH` で差し替えた
（複製側が本当に読まれていることを marker で自己検査済み）。複製は削除済み。
作業ツリーは無変更。`git stash` / `checkout` / `restore` は使っていない。
全実行で `CUDA_VISIBLE_DEVICES=` を立て、GPU には一切触れていない。

## verdict: request-changes

src の**振る舞い**に誤りは 1 件も見つからなかった（resume も実測で正しく動く）。
差し戻す理由は「検査が主張を支えていない」1 件と、運用者を壊れた設定へ導く案内 1 件、
成果物への秘密の混入 1 件。

---

## must-fix

### M1. resume の検査に検出力が無い（このリポジトリで 8 回目の同じ型）

- 対象: `tests/ml/paste_volume/test_train.py:596-642`（`TestInterruptionParity`）、
    `src/ml/paste_volume/train.py:341-343`
- 問題: `trainer.run(resume_from=config.resume.checkpoint, …)` を
    `resume_from=None` に変える変異で `test_train.py` は **30 passed（全緑）**。
    同じ場所へ `assert config.resume.checkpoint is None` を置く sanity 変異では 1 件落ちるので、
    変異が生きていることは確認済み。加えて `_experiment_logger` の `resume_run_id` 配線
    （実装者の判断ログ 2）を落とす変異も **38 passed（全緑）**。resume 経路は entrypoint 側で
    ひと通り無検査。
- 根拠: 参照 run も再開 run も同じ argv・同じ seed なので、**checkpoint を無視して最初から
    3 epoch 回しても参照と同じ最終重み・同じ `global_step`・同じ metric に到達する。**
    「一致する」型の assert が、再開したかどうかではなく「決定論であること」しか測っていない。
    対の `test_the_interrupted_run_had_not_reached_those_weights` は「中断時点の重みが違う」
    としか言わないので、再開が checkpoint を使ったかには触れない。
    （なお `final.pt` は signal 停止でも書かれる＝`_finalize` は stop_reason に依らず走るので、
    この対の検査自体が空振りしているわけではない。実測で確認済み。）
- **実装は正しい。** forward hook で数えると 通し **9** / 中断 **2** / 再開 **8**。
    `next_batch_index=1` で未 commit の 1 batch を踏み直して 8 になる（1+8=9 で辻褄が合う）。
    resume を無視する変異では再開が **9** になる。
- 直し方: すでにある counter を再開 run にも当て、**再開 run の学習 forward 回数が
    通し実行より少ない（実測 8 < 9）**ことを 1 行足す。または `main()` 経由で resume して
    MLflow の run_id が中断 run と同じであることを `test_train_mlflow.py` で見る。
- 確信度: 高（測定）。深刻度: 高

### M2. 運用者に案内している `logger.tracking_uri=file:///abs/mlruns` は必ず例外になる

- 対象: `src/ml/paste_volume/train.py:19` / `:402`、`src/ml/paste_volume/search.py:16` / `:177`
- 問題: MLflow 3.15.2 の file store は maintenance mode で例外（実測）。それでも
    `ExperimentLoggerConfig(tracking_uri='file:///…').validate()` は **None（合格）**を返し、
    `build().start()` で `MlflowException` になる。`logger=` を忘れた運用者が受け取る理由文
    （`_experiment_logger` / `_NO_LOGGER_REASON`）が、まさにその argv を案内している。
- 根拠: 実測。そのとおり打つと index / split / model / `config.json` を作ったあと
    `Trainer.run()` の内側で raw な例外に落ちる。entrypoint の「学習を始める前の不備は
    理由文字列で返す」契約（`run_training` docstring）からも外れる。step 4 は
    「sqlite なら `artifact_location` が要る」を `validate()` へ入れたのに、
    確実に動かない `file:` は素通しする。
- 直し方: 4 箇所の案内を `sqlite:////abs/mlflow.db` + `logger.artifact_location=/abs/mlartifacts`
    へ直し、`validate()` で `file:` と scheme なしを拒否する（理由に
    `MLFLOW_ALLOW_FILE_STORE` の存在を書く）。
- 確信度: 高（測定）。深刻度: 中〜高

### M3. `config.json` が tracking URI / storage URI を生のまま持ち、MLflow artifact として上がる

- 対象: `src/ml/paste_volume/experiment.py:350-359`（`save_experiment_config`）、
    `src/ml/paste_volume/train.py:507`（`_startup_artifacts`）
- 問題: `logger.tracking_uri=postgresql://mlflow:<pw>@…` /
    `hyperparameter_search.storage_uri=postgresql://optuna:<pw>@…` を渡して
    `save_experiment_config` すると、**両方の password が `config.json` にそのまま入る**（実測）。
    この file は run directory に残るうえ `_startup_artifacts` で MLflow へも上がる。
- 根拠: 同じ repo は param には `sanitized_tracking_uri` を使い（`train.py:597`）、
    study 成果物には `storage_uri_redacted` を使い、`test_the_document_keeps_the_storage_uri_
    redacted` で固定している。**新しく足した成果物だけが規約から外れている。**
- 現状 sqlite だけなので実害は無い。仕様書 §4 の「shared server へ移行」時に効く。
- 確信度: 高（測定）。深刻度: 中

---

## should-fix

### S1. `_LOCAL_DATABASE_SCHEMES` が MLflow の database backend と一致しない

`src/ml/paste_volume/experiment.py:67`。MLflow の DB scheme は
`['postgresql','mysql','sqlite','mssql']`（`mlflow.store.db.db_types.DATABASE_ENGINES` を実測）で、
**いずれも既定 artifact root は `./mlruns`**（`DEFAULT_LOCAL_FILE_AND_ARTIFACT_PATH` を実測）。
`postgresql://` / `mysql://` は `validate()` を素通りし（実測）、この field が防ごうとした
CWD 依存をそのまま踏む。逆に `duckdb:` は MLflow の tracking store として登録されていない。
過不足の両方向。確信度: 高（測定）

### S2. 既存 experiment では `artifact_location` が黙って無効になる

`src/ml/experiment/mlflow.py:170-180`。置き場所を決めずに作られた experiment があると、
`artifact_location` を渡しても `artifact_uri` は `<cwd>/mlruns/…` のまま（実測）。
docstring には「既存の experiment には効かない」と書いてあるが、`validate()` が sqlite で
**必須**にしている以上、運用者は指定した以上効いていると読む。
`test_the_artifacts_land_under_the_configured_location` は毎回まっさらな store なので
この経路を見ていない。**step 8 の実走で experiment `paste-volume` が以前に別 directory で
作られていたら、5 fold 全部の artifact が黙って CWD 側へ落ちる。**
直し方: 既存 experiment の `artifact_location` が要求と違えば理由を返す。
確信度: 高（測定）。深刻度: 中（step 8 に直接効く）

### S3. `artifact_location` は相対 path でも空文字でも通る

`experiment.py:193-200`。理由文は「（絶対 path）」と言うが検査は `is None` だけ。
実測: `artifact_location='mlartifacts'` も `''` も `validate() -> None`。
相対だと S2 と同じ CWD 依存が戻る。確信度: 高（測定）

### S4. `_calibrated` の理由が変数のシャドウで消える

`src/ml/paste_volume/train.py:479-484`。`before, reason = …` の直後に
`after, reason = …` で上書きするので、`before is None` かつ `after is not None` のとき
`(None, None)` ——理由なしの失敗——を返す。確信度: 高（読み取り）。深刻度: 低

### S5. calibration の失敗が 2 箇所で黙って捨てられる

`train.py:347`（`calibration, _ = _calibrated(…)`）と
`evaluate.py:251`（`calibration, _ = UncertaintyCalibration.load(…)`）。
`calibration.json` が壊れていても report は黙って `log_variance_offset=None` になる。
`test_dropping_the_calibration_changes_the_coverage` は「file が無い」場合だけを見ている。
「logging が黙って無効」を避ける方針（`run_training` docstring）と同じ形の穴。確信度: 高（読み取り）

### S6. `checkpoint=` が名指しした file を評価しない

`evaluate.py:134-137` は存在確認だけして親 directory を返し、`_restored_run` は必ず
role `best` を読む。実測: `checkpoint=<run>/final.pt` も `<run>/latest.pt` も
`best.pt` と同じ MAE 0.056215… を返す。中断 run の `latest.pt` を指したつもりで
best が測られる。確信度: 高（測定）

### S7. step 8 が実際に使う設定（`trainer=gpu` = compile ON）の end-to-end 経路が無検査

`test_train.py` / `test_evaluate.py` / `test_search.py` / `test_train_mlflow.py` の
すべてが `trainer.compile_enabled=false`。実装者自身が「compile ON + 可変 shape は
recompile 上限 8 に当たり以降 eager」と実測しているので、その状態で train → evaluate が
通ることは 1 度も測られていない（checkpoint の `_orig_mod` prefix は `test_task.py` が
見ているが、`config.json` → model 再構築 → `best.pt` load の経路は別）。確信度: 高（読み取り）

### S8. 「別プロセスから合流」は実際には同一プロセス

`test_search.py:235-257` `test_a_second_process_adds_trials_to_the_same_study` は
`run_search` を**同じプロセスで**呼ぶだけ。class docstring「同じ storage へ別プロセスから
合流できること」と test 名が実態より強い。
**読み出し側**（`test_another_process_reads_the_same_trials`）は本物の `subprocess.run` で
`optuna.load_study` する（trial 数 2 を `==` で見ており 0 では通らない）ので、
計画書 step 5 の「別プロセスから `collect` で読み戻す」は満たしている。
足りないのは書き込み側の合流。確信度: 高（読み取り）

### S9. `test_another_process_reads_the_same_trials` が後続 test の副作用に依存する

同じ module scope の storage へ `test_a_second_process_adds_trials_to_the_same_study` が
3 件目を積むので、`observed["trials"] == TRIAL_COUNT` は file 内の実行順に依存する。
`test_collect_reads_the_study_without_running_a_trial` は `>=` にしてあるので、
書き手は順序依存を意識している。1 件だけ `==` が残っている。確信度: 高（読み取り）

### S10. `evaluate` の state_dict 読み込みだけ例外を投げる

`evaluate.py:373` の `model.load_state_dict(dict(checkpoint.model_state))` は strict。
`config.json` と checkpoint が食い違うと `RuntimeError` になる。
`train._load_initial_weights` は同じ不整合をわざわざキー集合の比較で理由文字列にしている
（`train.py:445-453`）。entrypoint の契約が 2 つに割れている。確信度: 高（読み取り）

### S11. resume の異常系と正常系が entrypoint 側で未検査

計画書「異常系」の `resume.checkpoint` の dataset / config fingerprint 不一致に
entrypoint テストが無い。`main()` 経由で resume が成功する経路のテストも無い（M1 と対）。
確信度: 高（読み取り）

### S12. `test_search.py` / `test_train_mlflow.py` は GPU を掴む

`run_search` / `main()` は `device` を渡さないので `Trainer` が `_default_device()` で
CUDA を選ぶ（`loop.py:797`）。`test_train.py` / `test_evaluate.py` は `DEVICE=cpu` を渡している。
`make ml-docker-check` が実走と GPU を取り合う。確信度: 高（読み取り）

### S13. `train.main()` は signal / deadline 停止でも 0 を返す

`train.py:378-385`。docstring の 5 fold shell ループは、`SIGTERM` で途中終了した fold を
成功として次の fold へ進む。`stop_reason` は stdout に出るがループは読まない。
`stop_reason` を終了コードへ反映するか、docstring のループ側で見るかを決めたい。
確信度: 高（読み取り）。深刻度: 中（step 8 の運用形に効く）

### S14. `evaluate` と学習 loop が同じ数字を出すことを固定する検査が無い

実測では一致する（validation の `mean_absolute_error` / `root_mean_squared_error` /
`sample_count` / `valid_sample_count` が完全一致。`negative_log_likelihood` と
`one_standard_deviation_coverage` は calibration offset のぶんだけ違う）。
report が step 8 の判定に直結する以上、2 つの評価経路を 1 件で結び付けておきたい。
確信度: 高（測定）

### S15. 仕様書 §4 の記録項目のうち 4 つが載っていない

tag の **machine ID / parent base run ID / model schema version**、param の
**batch pixel budget**（`data.max_batch_pixels` / `max_batch_size`）。
`config.json` artifact には入っているので実害は小さいが、§4 の一覧との差分が記録に無い。
確信度: 高（読み取り）

### S16. 仕様書内の 2 つの記述のどちらへ寄せたのかが記録に無い

仕様書 line 85「run ID を共有できるよう**初期実装から server 経由に統一する**」と、
直 `sqlite:` URI を使う本実装は食い違う。line 739「local SQLite から shared server へ
同じ client API で移行できる」とは整合する。**`file://` が使えないこと自体は事実
（MLflow 3.15.2 で実測）**なので sqlite への変更は妥当だが、
`MLflowRunTarget.artifact_location` の追加はこの選択の副作用であり、
server 経由なら不要になる。どちらの記述を採ったのかを仕様書側へ書き戻したい。確信度: 高（測定 + 読み取り）

### S17. 申し送りの `tests/ml` 実測値が再現しない

実装者メモは「1799 passed, 1 skipped」。実測は **1720 passed, 0 skipped**（同じコンテナ、
`uv run pytest tests/ml -m "not hardware"`。`--collect-only` も 1720 で、CUDA の有無で変わらない）。
「+82 本」の増分（新規 test 関数 82 件）は合っている。
step 0/1 の M2、step 2/3 の M4 と同じ「memo の数字が再現しない」型の 3 例目。
確信度: 中（別の測り方をした可能性は残る）

### S18. `summarize_dataset` の「blank しかない dataset です」分岐にテストが無い

`cli.py:92-93`。確信度: 高（読み取り）

### S19. `search.main()` が lineage 検査を成果物を書いたあとに行う

`run_search` が `results_path` を書き終えたあと、`main` が print してから
`verify_lineage()` を見る（`search.py:162-172`）。紐付けが欠けても study.json は残る。
確信度: 高（読み取り）

---

## nit

- **N1.** `_train_measured_mean(data)` が `_run_params` と `_mean_bias_deviation` で 2 回走り、
    sample ごとに `entry_for` を 2 回引く（`train.py:624-632`）。
- **N2.** `run_training` と `main()` の両方が `config.validate()` を呼ぶ。
    同じ不整合に検出器が 2 つ（`run_training` docstring 自身の方針と逆向き）。
- **N3.** `cli._validation_lines` の docstring は「件数と理由を見せる」だが、rejection の
    理由は出していない。引数 `roots` も 1 行目にしか使わない。
- **N4.** `evaluate.py` が `UncertaintyCalibration` のために
    `ml.paste_volume.train` を import する。`CALIBRATION_FILE_NAME` は `experiment.py` にあるので、
    封筒も `experiment.py` 側が自然（評価 entrypoint が学習 entrypoint に依存しない形）。
- **N5.** `data.roots=[]` は空 tuple になり `evaluate_fold` の `if roots:` で黙って無視される
    （config の roots が使われる）。
- **N6.** `run_kind = "hpo-trial"` は仕様書 §4 の一覧に無い（`cell-split-train` / `fine-tune` も同様。
    §4 は `finetune`）。語彙が 2 箇所で別々に育っている。実装者も判断ログ 9 で自認。
- **N7.** `test_the_preset_and_the_trainer_profile_do_not_depend_on_the_argv_order` は
    `experiment=fine_tune` × `trainer=pi` の 1 組だけ。step 4 の申し送り 7 が警告したとおり
    `experiment/search.toml` が `[trainer]` を持つようになったので、preset × profile を走査で
    回すほうが規則に沿う（現状キーは重ならないので緑）。
- **N8.** `test_the_artifacts_land_under_the_configured_location` の
    `assert not (Path.cwd() / "mlruns").exists()` は process の CWD 依存。
    実際、私の b10 相当の変異で repo 直下に `mlruns/` が生まれた（削除済み）。
- **N9.** `collect_predictions` は `task.model.eval()` にして戻さない（`task.py:445`）。
- **N10.** `_repository_root()` は `parents[3]` 決め打ち。wheel install では別の場所を指す
    （`GitProvenance.capture` が理由を返すので害は無い）。
- **N11.** `run_training` は budget 拒否より前に `run_directory` を mkdir する。
    `test_a_refused_model_does_not_start_the_run` は `config.json` の不在しか見ない。

---

## 承認済み逸脱の再確認（orchestrator 依頼 2）

- **`data/` と `model/` の group directory を作らない**: 妥当。空 option を禁じる
    `test_every_layer_carries_at_least_one_key` と、木を固定する `PACKAGED_TREE` が対で効いている
    （変異 b5 相当を実装者が実測済み）。argv 規約とも矛盾しない ——
    `data` が conf root 直下の directory でないので `data.roots=…` は `key=value` として解決される。
    実測でも全 entrypoint テストがこの形で通っている。
- **`split_dimension` を既定値なしにする**: 妥当。`test_it_requires_an_experiment_preset` と
    `test_it_accepts_the_same_arguments_with_an_experiment_preset` の対で固定されている。
    step 1 の `sample_groups(dimension=…)` と同じ規則で、規則が 2 箇所で揃った。

## orchestrator の個別依頼への回答

| # | 依頼 | 結果 |
| --- | --- | --- |
| 1 | resume test は中断が no-op でも通らないか | 中断側は効いている（hook を no-op にすると 3 件落ちる）。**再開側は効いていない → M1** |
| 1 | `stop_reason == "signal"` を中断せずに出せるか | 出せない（`signals.requested` を要求）。hook 変異で当該 test も落ちる |
| 1 | `0 < next_batch_index < len(batch_plan)` の退化 | 実測 `0 < 1 < 3`。境界に乗っていない |
| 1 | `split=test` の拒否を常に真にしたら落ちるか | **13 件落ちる**（自己検査は効いている） |
| 1 | MLflow がモックされていないか / 空集合で通らないか | モックゼロ（`grep monkeypatch\|mock` が 0 件、実 sqlite + `MlflowClient`）。`len(history)==2` / `!= []` / 部分集合なので空では通らない。params / metrics / tags を黙って捨てる変異で 3 件落ちる |
| 1 | `search` の別プロセス合流 / trial 0 で通らないか | **読み出しは本物の subprocess**、`trials == 2` を `==` で固定。**書き込み側の合流は同一プロセス → S8**。順序依存 → S9 |
| 2 | step 4 の逸脱 2 件 | どちらも妥当（上記） |
| 3 | `file://` → sqlite の判断 | **事実**（MLflow 3.15.2 で `MlflowException` を実測）。§4 line 739 とは整合、line 85 とは食い違う → S16。`artifact_location` の追加も妥当だが穴が 3 つ → S1 / S2 / S3。**案内文が `file://` のまま → M2** |
| 4 | 計算量 gate は 159px x 5 view か | そう。`BUDGET_CROP_SIZE_PX=159` / `BUDGET_VIEW_COUNT=5`、記録 param も同じ。512x512x1view へ変える変異で **23 failed + 37 errors** |
| 5 | `run_directory` を分けなかったときの表面化 | `test_it_refuses_a_split_of_another_fold` が捕まえる。M3 の検査（`_held_out_mismatch`）を常に None にする変異で 1 件落ちる |
| 6 | `manifest.seed` を run seed に流していないか | 流していない。`seed_everything(config.trainer.seed, …)`。`manifest.seed` は `_split_fingerprint` と `split.seed` param だけ |
| 7 | `experiment/search.toml` が 60 epoch / patience 10 を与えるか | 与える。`test_the_search_preset_shortens_the_trial` が固定 |
| 8 | diff 内の docformatter 由来の日本語崩れ | **0 件**（変更 18 file を JP-空白-JP で走査。docformatter 再実行でも無変更） |

## 変異マトリクス（すべて `.review-mutants` 上で実測）

| # | 変異 | 落ちたテスト |
| --- | --- | --- |
| A1 | `trainer.run(resume_from=None)`（resume を無視） | **0 件**（M1） |
| A2 | `_experiment_logger` の `resume_run_id` を落とす | **0 件**（M1） |
| A3 | sanity: 同じ位置で `assert resume is None` | 1 件（変異が生きていることの自己検査） |
| A4 | 中断 hook を no-op に | 3 件 |
| A5 | `_held_out_mismatch` を常に None | 1 件 |
| A6 | `EvaluationRequest.validate` の拒否を常に真 | 13 件 |
| A7 | `collect_predictions` を `training=True` | 23 failed + 18 errors |
| A8 | 計算量 gate を 512x512x1view で測る | 23 failed + 37 errors |
| A9 | `_select_experiment` の `create_experiment` を消す | 1 件（repo 直下に `mlruns/` が生まれることも実測） |
| A10 | MLflow の `log_params` / `log_metrics` / `set_tags` を no-op | 3 件 |
| A11 | `cli` が mlflow / optuna を引く module を import | 1 件（`test_architecture.py`） |
| P1 | probe: 中断の進捗 | `stop=signal / counted=2 / epochs=0 / next=1 / plan_len=3 / final.pt あり / best.pt なし` |
| P2 | probe: 学習 forward 回数 | 通し **9** / 中断 **2** / 再開 **8**（resume 無視の変異では再開 **9**） |
| P3 | probe: train と evaluate の一致 | MAE / RMSE / 件数が完全一致。NLL と coverage は calibration offset のぶんだけ違う |
| P4 | probe: `checkpoint=final.pt` / `latest.pt` | どちらも `best.pt` と同じ MAE（S6） |
| P5 | probe: 既存 experiment + `artifact_location` | `artifact_uri` は `<cwd>/mlruns/…` のまま（S2） |
| P6 | probe: 秘密つき URI で `save_experiment_config` | password が `config.json` に平文（M3） |

## 検証結果

- format（凍結複製の中で `pre-commit run -a`）: **pass**
    （`check-added-large-files` の `uv.lock` だけ落ちるが、これは複製を新規 git repo にした
    副作用で本体では tracked。ruff / ruff-format / docformatter / codespell / mdformat は全て pass）
- type（`uv run pyright src/ml tests/ml scripts/ml_smoke.py`）: **pass**（0 errors, 0 warnings）
- test（`uv run pytest tests/ml -m "not hardware"`、CPU 固定）: **pass**
    （**1720 passed, 0 skipped** / 102 秒。実 5 session の opt-in 2 件も実行して緑）
- `make test` / `make run` / `pytest -m hardware` は実行していない
- GPU には一度も触れていない（全実行で `CUDA_VISIBLE_DEVICES=`）。
    `data/paste-volume-ml/` にも `/workspace/data/paste-volume-ml/` にも触れていない
