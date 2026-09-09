# paste_volume train/evaluate: step 5（entrypoint）+ step 6（MLflow）

branch `feature/2026-09-09/paste-volume-train-evaluate`、起点 `3cdc7a2`。

対象: `src/ml/paste_volume/{train,evaluate,search,cli}.py`、`experiment.py` と `task.py` へ追記、
`conf/experiment/search.toml` 新設、`conf/logger/mlflow.toml` 改稿、
`src/ml/experiment/mlflow.py` へ 1 field 追加、`tests/ml/paste_volume/*`、
`tests/ml/test_architecture.py`。

## 環境から出た確定事項（step 8 の実走に直接効く）

### 1. MLflow 3.15 の file store は例外になる。tracking URI は sqlite を使う

```text
MlflowException: The filesystem tracking backend (e.g., './mlruns') is in
maintenance mode and will not receive further updates. ...
If the filesystem backend is required, set MLFLOW_ALLOW_FILE_STORE=true
```

`logger.tracking_uri=file:///abs/mlruns` は **store を作る時点で落ちる**（run は 1 つも
残らない）。orchestrator の指示は `file://` だったが、環境が受け付けないので
**local sqlite** へ替えた。仕様書 §4 の「local SQLite から shared server へ同じ client API で
移行できる」がそのままこの形なので、方針とも一致する。

### 2. sqlite backend は成果物の置き場所を CWD から決める

実測: `mlflow.set_tracking_uri("sqlite:///...")` + `set_experiment(name)` で run を作ると
`artifact_uri` が **`<起動した directory>/mlruns/<id>/artifacts`** になる。起こした directory が
変わると同じ run の artifact が別の場所に生まれ、あとから辿れない。仕様書 §3 の
「環境変数や現在 directory を学習 core から暗黙参照しない」に正面から反する。

対応（計画外・要レビュー）:

- `MLflowRunTarget.artifact_location: str | None`（既定 None）を足し、指定があり experiment が
    未作成なら `create_experiment(name, artifact_location=...)` してから `set_experiment`
- `ExperimentLoggerConfig.artifact_location` を足し、**tracking URI が `sqlite:` / `duckdb:` の
    ときは必須**にした（server の URI では要求しない。artifact root は server 側の設定なので）

変異 b10（`create_experiment` の行を消す）で `test_the_artifacts_land_under_the_configured_location`
が落ち、実際に repository 直下へ `mlruns/` が生まれることも確認した。

### 3. compile ON + 可変 shape は recompile 上限に当たる

`trainer=gpu`（compile 既定 ON）で合成 3 session を回すと
`torch._dynamo hit config.recompile_limit (8)` が出て、以降 eager で走る。
理由は bucket ごとに shape が変わることと、`images` の dispatch key set が学習経路と評価経路で
違うこと（`AutogradCPU` の有無）。**計画書 R4 の「first-step 時間と遭遇 shape 数を記録し、
非現実的なら compile_enabled=false で測り直す」を step 8 で実際に判断すること。**
テストは `trainer.compile_enabled=false` で回している（compile 経路そのものは
`test_task.py` の parity が測る）。

## 計画外の判断（レビュー対象）

1. **`run_training` は model を組む前に `seed_everything(trainer.seed)` を呼ぶ。** model の
    初期化は `Trainer.run()` が seed を撒くより前に起きるので、撒かないと初期重みが process の
    周囲の乱数状態で決まる。fold ごとに違う重みから始まった run は metric を並べても比較
    できない。`TestSeeding` の 2 件が対で固定した（変異 b5 で 2 件が落ちる）。
2. **resume の run_id は checkpoint から引く。** `Trainer` は resume 元と logger の run_id が
    食い違うと拒否するので、`main()` が `resume.checkpoint` を読んで
    `ExperimentLoggerConfig.build(resume_run_id=...)` へ渡す。運用者に run ID を打たせない。
3. **`config.json` / `split.json` は run 開始と同時に記録先へ載せる。** `Trainer.run()` が run の
    開始と終了を持つので、呼び出し側に「run が開いている隙間」が無い。
    `TaggedExperimentLogger` を継承した `_StartupRecordingLogger` が start 直後に params と
    artifact を載せる。
    **その裏返しで `calibration.json` と `weights.pt` は MLflow へ載らない**（run が終わって
    いるので `log_artifact` が `RuntimeError`）。run directory には必ず残る。載せるには
    `Trainer` の run 所有を変えるか、同じ run を resume して開き直す必要があり、step 5 の
    範囲を超えると判断した。
4. **`collect_predictions` / `SplitPredictions` を `ml.paste_volume.task` へ置いた。** 学習後の
    calibration（train.py）と checkpoint の評価（evaluate.py）が同じ経路を通る必要があり、
    data と task を繋ぐのは task.py の役目。`GaussianObservation` は sample ID を持たないので、
    session slice の材料として `sample_ids` を並びごと返す。
5. **`FoldEvaluation` に計画書の 7 field へ 3 つ足した**: `run_name`（`folds=` が N directory を
    読むので、どの行がどの run か分からないと report が読めない）、`dataset_fingerprint`
    （fold 間の一致を report 側が要求する）、`session_slices`（計画書が指示した
    `CategoricalDimension` の結果。LOSO の test split では 1 本だが、その 1 本が
    「test に他 session が混ざっていない」ことの観測点になる）。
6. **`evaluate` は `ConfigComposition(overrides=argv)` で argv を構造化する。** group 層は積まない。
    評価に要るのは「どの run を、どの split で」だけで、experiment preset を選び直すと run が
    実際に使った設定と食い違う。TOML の値解釈・未知キー拒否・`key=value` の検査はすべて
    既存機構をそのまま使い、新しい parser を書いていない。
7. **`aggregate()` の標準偏差は不偏（n-1）、fold 1 個なら 0.0。** step 7 のベースライン表と
    比べるときは、あちらの sd の定義を確認すること。
8. **`weights.pt` は自前の封筒付き payload**（kind / schema_version / model_family /
    model_config / constraints / dataset_fingerprint / model_state）。`model.initial_weights` は
    この形式だけを受ける。キー集合が現在の model と食い違えば理由文字列で拒否する
    （`load_state_dict` の例外にしない）。
9. **`experiment/search.toml` は `run_kind = "hpo-trial"`。** 仕様書 §4 の run_kind 一覧
    （base-train / finetune / evaluate / export / benchmark）に無いが、既存の
    `cell_split.toml` も `cell-split-train` を使っており、一覧は網羅ではないと解釈した。
10. **`cli dataset validate` は隔離した cell（rejection）を失敗にしない。** 使えない cell を
    除いて index を組むのは設計どおりで、件数を出すに留める。session が読めないときだけ 1。

## 申し送りの訂正

step 4 の申し送り 3「`ImageConstraints.validate_augmentation` はまだどこも呼んでいない。
`run_training` が index を作った直後に呼ぶこと」は**誤り**。`PasteVolumeTrainingData.build`
（`task.py:180`）が既に `smallest_source_size=index.smallest_source_size` で呼んでいる。
`run_training` で二重に呼ぶと同じ不整合へ検出器が 2 つ並ぶので、呼んでいない。

## 変異と落ちたテスト

無変異の baseline: `test_train.py` 30 / `test_evaluate.py` 18 / `test_search.py` 13 /
`test_cli.py` 13 / `test_train_mlflow.py` 8。

### (a) 検査器を壊す

| 変異 | 落ちたテスト |
| --- | --- |
| a1 `_sessions_of` が常に空集合を返す | **2**（held-out 隔離と、その自己検査の両方） |
| a2 中断 hook を no-op にする（SIGTERM を送らない） | **3**（`test_the_hook_interrupts_...` / resume 一致 / 「中断時点では違う重み」） |
| a3 `_assert_same_weights` を key 集合の比較だけにする | **1**（`test_the_interrupted_run_had_not_reached_those_weights`。重み比較が空洞化したことを負の検査が捕まえる） |
| a4 `_session_values` が定数 label を返す | **2**（test split の slice と validation split の slice） |

`a4` の前段として「テスト側で slice の値を同語反復に書き換える」変異も試したが**全緑**だった。
1 本の test 内で観測器を弱めても、**同じ観測器を逆向きに使う対のテスト**（validation split は
held-out を含まない）が生きているので、production 側を壊す a4 の形でしか殺せない。
検査の強度は観測器そのものに掛かっていることの実測になっている。

### (b) 検査対象へ自然に書かれうる別の形を注入する

| 変異 | 落ちたテスト |
| --- | --- |
| b1 `split=test` の `allow_frozen_test` 要求を消す | **1**（`test_it_refuses_the_test_split_without_the_flag`） |
| b2 calibration の offset を読むが log 分散へ足さない | **1**（`test_dropping_the_calibration_changes_the_coverage`） |
| b3 `fine_tune` の freeze を掛け忘れる | **1**（`test_freezing_shows_up_in_the_trainable_parameter_count`） |
| b4 計算量 gate を 512x512 x 1 view で測る（仕様書の旧記述） | **21**（全 run が学習前に拒否される） |
| b5 model を組む前の `seed_everything` を消す | **2**（`TestSeeding` の 2 件） |
| b6 split.json を run directory へ残さない | **4**（成果物・fold 取り違え検出・resume 系） |
| b7 trial ごとの `run_directory` を分けない | **1**（`test_each_trial_gets_its_own_run_directory`） |
| b8 `collect(experiment_run_ids=...)` の紐付けを落とす | **1**（`test_every_trial_names_its_experiment_run`） |
| b9 blank を学習 sample と同じ母数に入れる | **2**（件数と体積範囲） |
| b10 `create_experiment(artifact_location=...)` を消す | **1**（`test_the_artifacts_land_under_the_configured_location`。repository 直下に `mlruns/` が生まれることも実測） |

## docformatter 由来の日本語崩れ

orchestrator が指定した 7 箇所（`test_task.py:343/812/813/834/847`、`helpers.py:190/254`）を
直した。**直し方は「段落を 1 物理行 72 文字以内」だけでは足りない**ので手順を残す。

1. docformatter は段落を join するとき**改行を空白へ置換する**。日本語の行末と次行の行頭が
    どちらも日本語だと、そこに空白が 1 つ生まれる（これが崩れの正体で、文字は化けていない）
2. 一度 join されたあとの再 wrap は**既存の空白位置でしか折らない**ので、崩れは初回だけ
3. したがって手順は「pre-commit を 1 度通す → **日本語文字どうしの間の空白だけ**を消す →
    もう 1 度通して無変更を確認」。今回この手順で 22 箇所を直し、2 巡目で再発 0 を確認した
4. **`\s` で消してはいけない。** 最初に `(?<=日本語)\s(?=日本語)` で消したら改行まで畳んで
    既存 docstring を 1 行へ潰した（`git checkout` で巻き戻し済み）。対象は **U+0020 だけ**
5. 整形前後で **非 ASCII 文字の多重集合が一致すること**を全 file で機械確認した（漢字化けなし）

**未対応（本 MR の diff 外、別タスク提案）**: 同じ形の崩れが `tests/ml` / `src/ml` の他 file に
**28 箇所 / 12 file** 残っている（`test_index.py` 6、`test_batch.py` 3、`test_runner.py` 2、
`artifact/document.py` 1 など）。検出は次で足りる。

```bash
grep -rnP '[ぁ-んァ-ヴー一-龥、。]  ?(?<= )[ぁ-んァ-ヴー一-龥]' --include=*.py src tests
```

## 実データでの実測（step 8 の見積もり）

`data/paste-volume-datasets`（5 session）に対して entrypoint を実際に走らせた。

`cli dataset summarize` の出力は計画書の表と完全に一致した。

| 項目 | 値 |
| --- | --- |
| session / sample / blank | 5 / 816 / 16 |
| rejection | 0 |
| **cell group** | **167**（計画書の 166 と 1 違う。purge cell の座標を含むため） |
| measured [uL] | min 0.0324 / max 0.3625 / mean 0.1652 |
| dataset fingerprint | `sha256:072f857ffdc9deeb5ad2c2a240e9f4442d3e022c14e4d595332e09f5b59ef53f` |

1 fold（held-out `...20260908T144137.001+0900`、train 3 session）の所要時間、
RTX 4090 / `trainer=gpu`（AMP、`deterministic=true`）:

| 設定 | 6 epoch の wall | 1 epoch あたり |
| --- | ---: | ---: |
| `compile_enabled=false` | 17 秒（起動込み） | 約 2 秒 |
| `compile_enabled=true`（inductor cache が温かい 2 回目） | 25 秒 | 約 2 秒 |
| `compile_enabled=true`（cache が冷たい初回） | 2 epoch で 78 秒 | 初回 compile に約 60 秒 |

- **200 epoch は 1 fold あたり 7〜8 分、5 fold で 40 分程度**の見込み
- **`deterministic=true` のまま CUDA で走る**（未対応 op に当たらない）
- **compile の steady-state は eager と同じ**。この規模では compile の取り分が
    ほぼ無く、初回 60 秒を足すだけになる。計画書 R4 の判断材料として、
    実走 1 本目の `epoch_seconds` を見て決めてよい
- 6 epoch だけの試走で test MAE 0.0683（定数予測 0.069755 とほぼ同じ）。
    **200 epoch の結果ではないので判定には使えない**が、経路は最後まで通る

## 検証

- `make ml-docker-check`: pre-commit（全 hook）→ pyright（`src/ml tests/ml scripts/ml_smoke.py`）
    → `pytest tests/ml` をすべて通した
- `tests/ml` 全体: **1799 passed, 1 skipped**（本 MR で +82 本）
- `make test` / `make run` / `pytest -m hardware` は実行していない
