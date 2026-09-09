# paste_volume train/evaluate step 4（packaged config）

対象: `src/ml/paste_volume/conf/**`、`src/ml/paste_volume/experiment.py`、
`tests/ml/paste_volume/test_conf.py`。step 5 以降と `model.py` / `task.py` には触っていない。

## 作った TOML 階層

```text
src/ml/paste_volume/conf/
├── base.toml                            # [trainer] 骨格（max_epochs / monitor / lr / compile）
├── experiment/base.toml                 # [data] split_dimension = "session"
├── experiment/cell_split.toml           # run_kind + [data] split_dimension = "cell"
├── experiment/fine_tune.toml            # run_kind + [data] + [model] + [trainer]
├── hyperparameter_search/base_optuna.toml  # [hyperparameter_search] search space 4 本
├── logger/mlflow.toml                   # [logger] experiment_name
├── trainer/gpu.toml                     # [trainer] AMP
└── trainer/pi.toml                      # [trainer] accumulation 4 / deadline 3300
```

`uv build --wheel` で 8 枚とも wheel に入ることを実測した（`ml/paste_volume/conf/**.toml`）。
`pyproject.toml` の変更は不要。`[tool.uv.build-backend] module-name` に `ml` が入っており、
uv_build は module directory 配下の非 Python file をそのまま含める。

## 計画外の判断ログ

### 1. `data/paste_volume.toml` と `model/resnet_small.toml` を作らなかった

計画書の階層から 2 枚減らした。理由はどちらも「書ける内容が既定値の再掲だけ」になるため。

- `data`: 計画書の意図的逸脱で `manifest` を落としたので、この group が持てるのは `roots`
    だけになる。`roots` は機械固有の絶対 path で、仕様書 §3 が「環境変数や現在 directory を
    学習 core から暗黙参照しない」と決めている以上、リポジトリ相対の既定値も置けない
- `model`: 既定値が v1 の確定値そのものなので差分が 0 件。計画書自身が「ほぼ空 + コメント」と
    書いていた

**空の option file を置かない規約をテストで固定した**（`test_every_layer_carries_at_least_one_key`）。
選んでも何も起きない option は読み手を誤らせる。将来 v2 encoder や composite manifest が
入ったときに、そのとき初めて group を足す。

### 2. `PasteVolumeDataConfig.split_dimension` に既定値を持たせなかった

計画書のシグネチャは `split_dimension: SplitDimension = "session"`。既定値を外した。

- step 1 が `index.sample_groups(dimension=...)` を「keyword 必須・既定値なし」にしたのと
    同じ理由。どちらの次元で汎化を測るのかは、この MR の設計判断そのもの
- 結果として **`experiment=` の選択が必須**になる（唯一 `split_dimension` を書く層なので、
    省くと `required field missing @ $.data.split_dimension` で run 開始前に落ちる）。
    仕様書 §7 の argv 例はいずれも `experiment=` を渡しており、運用形と一致する
- `experiment/base.toml` に書く内容が生まれるのも副次的な利点（`run_kind` を必須にして
    同じ効果を得る案も検討したが、強制したい選択は run 種別ではなく split 次元）

### 3. `SearchConfig` を `ml.paste_volume.search` ではなく `experiment.py` に置いた

計画書は `SearchConfig` を `search.py`（step 5）に置いている。`PasteVolumeExperimentConfig`
が `hyperparameter_search: SearchConfig | None` を持つ以上、そこへ置くと
`experiment.py` ⇄ `search.py` の循環 import になる。**step 5 の `search.py` は
`from ml.paste_volume.experiment import SearchConfig` で受け取ること。**

### 4. 機械固有の値は同梱 conf に書かず、既定値のない必須 field にした

`data.roots` / `logger.tracking_uri` / `hyperparameter_search.storage_uri` の 3 つ。
計画書は `logger/mlflow.toml` に「file: tracking URI」を書く想定だった。

相対 URI を既定にすると起動した directory ごとに MLflow store が分かれ、5 fold の run が
別々の場所へ散る。sqlite storage は `StudyStorage.validate` が絶対 path を要求する。
どちらも「同梱 config が持てない値」なので、構造化の段階で落ちる形にした。

`run_directory` は既定 `Path("runs")` のまま（作る側の出力先であって、外部システムの所在では
ないため）。

### 5. `monitor = "negative_log_likelihood"`

仕様書 §3 の「early stopping: validation NLL」「model 選択は calibration 前の validation NLL
最小」「objective は最小 validation NLL」に合わせた。`PasteVolumeTask.reduce` が
`GaussianRegressionMetrics` の field 名でこの key を返す。

### 6. `ExperimentLoggerConfig.run_target()` を public にした

計画書は `validate` / `build` だけ。`MLflowRunTarget` の検証規則を複製しないために内部で
必要で、step 5 が `sanitized_tracking_uri` を param へ載せるのにも要るので public にした。

### 7. `_duplicated_defaults` の入れ子の辿り方を強化した

`tests/ml/tuning/test_integration.py` の手口をそのまま持ってくると、**既定値を持たない
必須 field（`data` / `trainer`）の中身が丸ごと走査から漏れる**（元の実装は入れ子クラスを
`type(field.default)` から得るため）。annotation（`get_type_hints`）から辿る版に変えた。
この差は変異 a4 で実測している（下記）。

## 当てた変異と落ちたテスト

`docker compose ... exec -T ml uv run pytest tests/ml/paste_volume/test_conf.py` で実測。
無変異は 61 passed。

### (b) 検査対象へ自然に書かれうる別の形を注入する

| 変異 | 落ちたテスト |
| --- | --- |
| b1 `experiment/base.toml` の `[data]` へ typo key `held_out_sessions = "x"` を 1 行足す | **21 件**（`test_every_option_structures_strictly` ほか、`experiment=base` を合成する全テスト） |
| b2 `trainer/edgey.toml` を `edge.toml` と同じ root 直書きの形で 1 枚増やす | 4 件（`test_it_lists_the_expected_groups_and_options` / `test_every_option_file_keeps_its_shape` / 構造化 2 件） |
| b3 `trainer/gpu.toml` へ attrs 既定値 `weight_decay = 1.0e-4` を書く | 1 件（`test_no_layer_repeats_an_attrs_default`） |
| b4 新しい group directory `runner/local.toml` を足す | 5 件（`test_every_group_is_a_root_field_or_a_registered_preset` ほか） |
| b5 コメントだけの `model/resnet_small.toml` を足す（計画書が想定した形） | 3 件（`test_every_layer_carries_at_least_one_key` ほか） |

### (a) 検査器を壊す

| 変異 | 落ちたテスト |
| --- | --- |
| a1 `_duplicated_defaults` を常に `[]` にする | 4 件（`test_it_detects_a_default_written_into_a_layer` の全 param） |
| a2 `_shape_offenders` を常に `[]` にする | 7 件（`test_the_shape_check_rejects_*` の全 param） |
| a3 走査対象を空にする（conf root を空 directory へ向ける） | 10 件（範囲固定の `checked != []` と木の pin） |
| a4 入れ子の辿りを `type(field.default)` に戻す（tuning helper と同じ弱さ） | 3 件（`required-nested-field` / `required-nested-field-of-the-domain` / `two-levels-of-nesting`） |

a4 が「強化していなければ `[data]` と `[trainer]` の既定値二重定義は素通りしていた」ことの
実測になっている。

## 他 implementer への IF 変更通知

`src/` で触ったのは新規 file `experiment.py` と `conf/**` だけ。既存 module の公開 IF は
変えていない（`task.py` / `model.py` は import するだけ）。

## step 5（entrypoint）への申し送り

1. **`compose_experiment` は `validate()` を呼ばない。** 構造化できたことと学習を始めて
    よいことは別。`main()` が `compose_experiment` → `config.validate()` の順で呼び、
    どちらの理由も stderr へ出すこと
2. **`SearchConfig` の import 元は `ml.paste_volume.experiment`**（上記 3）
3. **`ImageConstraints.validate_augmentation(augmentation, smallest_source_size=...)` は
    まだどこも呼んでいない。** 源画像の最小辺は index を組むまで分からないので、
    `run_training` が `PasteVolumeSampleIndex` を作った直後に呼ぶ
4. **HPO trial の「最大 60 epoch / early stopping patience 10」（仕様書 §3）はどの層にも
    入っていない。** `hyperparameter_search/*.toml` は top-level key が group 名 1 つという
    規約があるので `[trainer]` を書けない。`experiment/search.toml` を足すか、`search.py` が
    `trainer.max_epochs=60` を override として積むかを step 5 で決めること（計画書の file
    一覧に無いので step 4 では作らなかった）
5. **`logger` を選ばないと `config.logger is None`。** 無指定でも動く no-op logger は
    `ml.experiment` に無い。`run_training(logger=...)` を必須引数にしてあるので、`main()`
    が None のときどうするかを決める必要がある
6. `SplitManifest.seed` に入る 64 bit の fold 派生値を `np.random.seed` へ流さないこと
    （前レーンからの申し送りをそのまま引き継ぐ）。`TrainerConfig.seed` は別物で、既定 0
7. **argv の順序**: `ConfigComposition` は group 層を argv の順に積む。現状 `experiment`
    preset と `trainer` profile は同じキーを持たないので順序非依存で、それを
    `test_the_preset_and_the_trainer_profile_do_not_depend_on_the_argv_order` が固定して
    いる。将来 preset に `[trainer] max_epochs` を足すとこの性質が壊れる
8. `run_directory` の既定は相対 `runs`。fold ごとに違う値を渡さないと 5 fold が同じ
    directory を共有し、step 0/1 レビューの M3（既存 split.json の食い違い）を踏む

## 検証結果

- format（`pre-commit run --files <自分の file>`、コンテナ内）: pass。docformatter の
    再実行で差分が出ないことも確認済み（日本語の化けなし。summary を英小文字で始めると
    大文字化されるので、すべて日本語始まりへ直した）
- type（`uv run pyright`、全体）: `ml/` と `tests/ml/` に指摘 0。残る 35 errors は
    `pcbasm` / `tests/helpers` の `pcbnew` / `picamera2` 未解決で、学習コンテナに装置依存が
    無いことによる既存の状態
- test: `pytest tests/ml/paste_volume/test_conf.py tests/ml/config tests/ml/test_architecture.py
    tests/ml/tuning -m "not hardware"` → **355 passed**
- `make ml-docker-check` と `pre-commit run -a` は並行レーンの未完成 file を壊すので実行して
    いない（orchestrator が合流後に行う）
