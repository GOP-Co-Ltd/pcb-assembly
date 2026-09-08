# MR5（ML 基盤の設定合成 + ハイパーパラメータ探索）の仕様テスト

計画: `memory/agents/implementation-planner/ml-core-5-config-tuning.md`
裁定: `memory/agents/orchestrator/ml-core-5-config-tuning.md`

`plan-implementer` と並列で書いた。`src/**` は一切触っていない。

## 書いたテスト一覧（collect 済みの件数）

| ファイル | 件数 | 計画の対応 |
| --- | --- | --- |
| `tests/ml/config/test_composition.py` | 33 | §13.1 / §15.1 / 裁定 1 |
| `tests/ml/config/test_packaged.py` | 8 | §13.2 / §15.2 |
| `tests/ml/tuning/test_study.py` | 45 | §13.3 / §15.3 / 裁定 3 |
| `tests/ml/tuning/test_search_space.py` | 34 | §13.4 / §15.4 / 裁定 4 |
| `tests/ml/tuning/test_runner.py` | 24 | §13.5 / §15.5 / 裁定 2 |
| `tests/ml/tuning/test_integration.py` | 11 | §15.6 |
| `tests/ml/test_serialization.py` | 28（うち新規 12） | 裁定「案 A」（Scalar union hook） |
| `tests/ml/test_architecture.py` | 4（既存。層登録と assertion の実効化） | §6 / §14 commit 1 の 4 / §15.7 |

新規 package: `tests/ml/config/__init__.py`、`tests/ml/tuning/__init__.py`（docstring のみ）。

## テスト ↔ §16 の対応行 ↔ 潰す機構

合流後の変異実験はこの表をそのまま使える。**§16 の太字 12 行はすべてテスト化した。**

| §16 | テスト | 潰す機構 |
| --- | --- | --- |
| 1 | `test_composition.py::TestComposeLayers::test_merges_later_layers_over_earlier_ones_recursively` | `_merge_into` の再帰分岐を素の `dict.update` にする |
| 2 | `TestComposeLayers::test_replaces_lists_instead_of_concatenating_them` | list を連結するようにする |
| 3 | `TestComposeLayers::test_reports_a_malformed_toml_layer` | `tomllib.TOMLDecodeError` の捕捉を外す |
| 4 | **削除**（裁定 5。到達不能分岐なので実装から削る） | — |
| 5 | `TestApplyOverrides::test_creates_missing_intermediate_tables` | `_assign` の中間 table 生成を削る |
| 6 | `TestApplyOverrides::test_reports_an_intermediate_key_that_is_not_a_table` | `_assign` の `isinstance(child, dict)` 判定を削る |
| 7 | `TestApplyOverrides::test_interprets_values_with_the_toml_scalar_rules`（7 ケース） | TOML として解釈できる token（`true` / `1.0e-4` / `7` / `"x"` / `[1, 2]`）の解釈をやめて全部 str にする。裸の文字列ケース（`mae` / `max`）は逆に「str へ落ちること」を守る（裁定 1）。`type()` を固定しているので `true` → `"true"` でも落ちる |
| 8 | `TestApplyOverrides::test_reports_a_token_without_a_separator` / `test_reports_a_token_with_an_empty_key` | `if not separator or not key` を削る |
| 9 | `TestFromArguments::test_treats_a_token_whose_name_is_not_a_directory_as_an_override` | 判定を「`.` を含むか」に変えると `run_kind=finetune` が group 扱いになって落ちる |
| 10 | `TestFromArguments::test_stacks_base_layers_before_group_layers` | 層の連結順を group → base に入れ替える。`layer_paths` の並びと `shared` の勝ち負けの両方で落ちる |
| 11 | `TestStructure::test_rejects_an_unknown_key` | `structure_strictly` ではなく素の `converter.structure` を呼ぶ |
| **12** | `TestStructure::test_rejects_an_integer_for_a_float_field`（対照 `test_accepts_a_float_field_written_with_a_decimal_point`） | 合成層に int → float 昇格を入れる（決定 3 の検出器） |
| 13 | `test_packaged.py::TestPackagedConfiguration::test_locates_an_existing_directory` | `conf/` を `module-name` の外へ移す／`locate` のパス計算をずらす |
| 14 | `TestPackagedConfiguration::test_lists_the_edge_option_of_the_trainer_group` | `conf/trainer/edge.toml` を削除する |
| 15 | `test_study.py::TestStudyIdentity::test_changes_the_name_when_any_element_changes`（3 ケース） | `study_name` の組み立てから該当要素を落とす。`search_space_fingerprint` を外すケースが独立に落ちる |
| 16 | `TestStudyIdentity::test_sanitizes_the_model_family_and_truncates_the_fingerprints` | `re.sub(r"[^a-zA-Z0-9_.-]+", "-", ...)` を削る／切り出し桁数を変える |
| 17 | `TestStudyStorage::test_reports_storage_that_cannot_be_shared[sqlite:///study.db]` | `is_absolute()` 判定を削る |
| 18 | 同 parametrize の `sqlite://` / `redis://` / `postgresql://host` ケース | scheme 許可リスト・host / database 必須判定を削る |
| **19** | `TestStudyStorage::test_redacts_the_password` | `redacted_uri` が `self.uri` をそのまま返すようにする |
| 20 | `TestStudyStorage::test_keeps_the_port_in_the_redacted_uri` | `redacted_uri` の port 連結を落とす |
| 21 | `TestStudyResults::test_selects_the_best_trial_by_direction`（minimize / maximize） | `best_trial` を常に `min` にする |
| 22 | `TestStudyResults::test_does_not_consider_incomplete_trials_as_best` | `is_complete` の絞り込みを削る |
| **23** | `TestStudyResults::test_never_writes_a_credential_into_the_document` | `StudyResults` に生 `storage_uri` を足して `build` で埋める。生文字列検索なのでフィールド名を変えても落ちる |
| 24 | `TestStudyResults::test_round_trips_through_a_document` / `test_reports_an_unsupported_schema_version` | **変異実験で訂正（orchestrator 承認）。** 「`DOCUMENT_KIND.schema_version` を上げて `load` 側だけ据え置く」は変異として成立しない（save / load が同じ `DocumentKind` を共有するので往復してしまう）。実際に機構を守っているのは `DocumentKind.load` の `schema_version` 照合で、これを削ると本行のテストと `tests/ml/artifact/test_document.py` が落ちる |
| **25** | `TestVerifyLineage::test_reports_the_numbers_of_completed_trials_without_a_run_id` | `verify_lineage` を常に `None` を返すようにする |
| 26 | `TestVerifyLineage::test_allows_a_missing_run_id_on_failed_or_pruned_trials` | 絞り込みを外して全 trial に run id を要求する |
| 27 | `test_search_space.py::TestParameterDistributionValidation::test_reports_an_inconsistent_distribution`（14 ケース） | `validate()` の該当分岐を 1 つずつ削る。ケース単位で落ちる |
| 28 | `TestSuggest::test_suggests_an_integer_on_the_step_grid` | `suggest_int` へ `step` を渡すのを止める（`low=2, high=10, step=4` なので格子外の値が出る） |
| 29 | `TestSuggest::test_suggests_a_logarithmic_float_inside_the_range` | `suggest_float` へ `log` を渡すのを止める（範囲チェックのみなので検出力は弱い。§16 と同じ限界） |
| **30** | `TestSearchSpaceFingerprint::test_ignores_the_insertion_order_of_the_parameters` | `fingerprint` を `sort_keys=False` の `json.dumps` にする |
| 31 | `TestSearchSpaceFingerprint::test_changes_when_the_logarithmic_flag_changes` | `fingerprint` の対象から `log` を落とす |
| 32 | `test_runner.py::TestTrialAssignment::test_prefixes_every_tag_with_the_search_namespace` | `hpo.` 前置きを外す |
| 33 | `TestTrialAssignment::test_produces_override_arguments_that_the_composition_accepts` | 区切りを `=` から `:` に変える |
| **34** | `TestHyperparameterSearchRun::test_joins_trials_from_two_instances_sharing_one_storage` | `create_study` の `load_if_exists=True` を外す |
| **35** | `TestHyperparameterSearchRun::test_resumes_an_existing_study_without_reducing_the_trial_count` | 同上、および `trial_count` を残 trial 数へ減算する（MR185 の再現。3 インスタンス目の呼び出し回数 4 と総数 9 の両方で落ちる） |
| 36 | `TestHyperparameterSearchRun::test_does_not_create_a_study_when_validation_fails`（3 ケース） | `run` 先頭の `if error := self.validate()` を削る。SQLite ファイルの不在も見る |
| 37 | `TestHyperparameterSearchRun::test_records_a_failing_trial_without_failing_the_whole_search` | optuna の `catch` 指定を外して `run` 全体を失敗にする |
| 38 | `TestCollect::test_reads_an_existing_study_without_running_trials` / `test_leaves_the_experiment_run_ids_empty_by_default` | `collect` が `study.optimize` を呼ぶようにする |
| **39** | `test_integration.py::TestDefaultsAreNotDuplicated::test_no_packaged_option_repeats_an_attrs_default` | `conf/trainer/edge.toml` に attrs の既定値と同じ値の行を 1 行足す |
| **40** | `TestFingerprintClassificationSurvivesComposition::test_two_layers_differing_only_in_the_time_budget_share_a_fingerprint` | `ml/training/loop.py` の `_FINGERPRINT_EXCLUDED_FIELDS` から 1 要素を削る |
| **41** | 同 `::test_two_layers_differing_in_the_learning_rate_do_not_share_a_fingerprint` | `fingerprint` を定数にする |
| 42 | 同 `::test_the_time_budget_reaches_the_tags_with_the_training_prefix` / `::test_the_time_budget_never_reaches_the_params` | `as_tags` を空 dict にする／`as_params` の除外を外す |
| **43** | `TestSearchedValuesDoNotEnterParams::test_search_metadata_never_shares_a_key_with_the_params`（対照 `::test_the_searched_value_itself_is_recorded_as_a_param`） | `as_tags()` の内容を `as_params()` へ合流させる（`hpo.study_name` を param 側へ移す） |
| 44 | `TestComposeThenSearch::test_runs_a_search_over_the_packaged_configuration` / `::test_saves_a_document_whose_lineage_verifies` | 通し経路なので広く落ちる。単独の機構検証には使わない |
| **45** | `test_architecture.py::TestDependencyFreeLayer` | `ml/tuning/study.py` に `import optuna` を足す（`DEPENDENCY_FREE_MODULES` へ登録済み） |
| 46 | `test_architecture.py::TestRuntimeLayer` | `ml/training/loop.py` に `import optuna` を足す |
| 47 | `test_architecture.py::TestDomainIndependence` | `ml/config/composition.py` に `from pcbasm.config import Machine` を足す |

### 追加行（裁定「案 A」= Scalar union hook。§16 に追記が必要）

| # | テスト | 潰す機構 |
| --- | --- | --- |
| 48 | `test_serialization.py::TestScalarUnion::test_structures_scalar_containers` | `make_strict_converter` の `register_structure_hook(bool \| int \| float \| str, ...)` を削る。`Mapping[str, Scalar]` / `tuple[Scalar, ...]` が `Unsupported type` で落ちる。§16 #24（`StudyResults` の save→load）と §15.4 の categorical TOML も連動して落ちる |
| 49 | `TestScalarUnion::test_rejects_a_subtype_of_int` | `_structure_scalar_union` の `type(value) in _SCALAR_TYPES` を消して `isinstance` だけにする（`IntEnum` が通る） |
| 50 | `TestScalarUnion::test_keeps_the_exact_scalar_type` / `test_round_trips_without_promoting_integers_to_floats` | hook が値を `float(value)` などに正規化するようにする |
| 51 | `TestScalarUnion::test_rejects_values_that_are_not_scalars` | hook の `raise` を削って値をそのまま返す |
| 52 | `test_search_space.py::TestSearchSpaceFromToml::test_structures_categorical_choices` | 上記 #48 と同じ。categorical 分布を TOML から宣言できなくなる |
| 53 | `test_integration.py::TestRegisteredGroups::test_every_registered_group_is_packaged` / `test_packaged.py::TestPackagedOptionsStructure::test_every_group_has_a_registered_target` | 同梱 group を増やして `PACKAGED_GROUP_TARGETS` へ登録しない（#39 の走査が空振りするのを防ぐ機構） |

### 裁定 1〜4 で増えた行（§16 へ追記が必要）

| # | テスト | 潰す機構 |
| --- | --- | --- |
| 54 | `test_composition.py::TestApplyOverrides::test_accepts_a_bare_string_without_quotes` | 裸の文字列を TOML として解釈できないと拒否する実装へ戻す（裁定 1 の回帰検出器）。`monitor=mae` が通らなくなる |
| 55 | `TestStructure::test_rejects_a_bare_string_for_an_integer_field` | **裁定 1 で機構が合成層から converter 側へ移った行。** `structure_strictly` を素の `converter.structure` に変えると `"abc"` が int へ暗黙変換されうる |
| 56 | `TestStructure::test_accepts_a_bare_string_for_a_literal_field` / `test_rejects_a_string_outside_a_literal_field` | 裸の文字列を str にしない／`Literal` の検証を外す |
| **57** | `test_runner.py::TestHyperparameterSearchValidation::test_reports_a_search_space_that_does_not_match_the_identity` | `validate()` から `identity.search_space_fingerprint == search_space.fingerprint` の照合を削る（裁定 2。異なる探索空間の trial が 1 study へ合流するのを防ぐ機構） |
| **58** | `test_study.py::TestStudyStorage::test_drops_every_place_a_credential_can_hide`（3 ケース）と `TestStudyResults::test_never_writes_a_credential_into_the_document`（3 ケース） | `redacted_uri` から query / fragment の除去を落とす（裁定 3。userinfo だけ落とす実装では `?password=secret` と `#secret` が残る） |
| 59 | `test_search_space.py::TestParameterDistributionValidation`（`low=0.5` / `high=10.5` の 2 ケース） | `kind="integer"` の境界を `int()` で黙って切り捨てる（裁定 4。`low=0.5` が `0` に化ける） |

**裁定 2 に伴うテスト側の変更。** `test_runner.py::_identity` と
`test_integration.py::TestComposeThenSearch._search` は、`search_space_fingerprint` を
固定 hex ではなく `SearchSpace.fingerprint` から採るように直した。固定 hex のままだと
`validate()` が全 run を拒否する。

## §16 でテスト化できなかった行

- **#4「最上位が table でない TOML」** — `tomllib` の root は常に mapping を返すので、公開
    インターフェース経由でこの入力を作れない。**裁定 5 により実装から削除**（AGENTS.md
    「起こり得ないシナリオ向けの処理を増やさない」）。§16 の対応表からも落とした。
- **#29（`log=True`）** — 範囲チェックしか書けず、`log` を落としても偶然範囲内に入るので
    検出力が弱い。**裁定により現状で可**。`log` の区別は fingerprint 側（#31
    `test_changes_when_the_logarithmic_flag_changes`）が守っているので二重には持たない。

## 期待される失敗（記録時点では全て解消）

書いた時点では `ml.tuning.*` が未実装で `ModuleNotFoundError` だった。`plan-implementer` の
実装が入った後に再実行し、**現在は全て緑**。

- 実行結果: `tests/ml` = **756 passed / 1 skipped**（ベースライン 592 passed / 1 skipped、+164）
- pyright: `src/ml tests/ml` で 0 errors
- 整形: `ruff format --check` / `ruff check` / docformatter `--check`（いずれも pre-commit と
    同じ pin rev の実体）で `tests/ml` 全 42 file 差分なし

## §13 のシグネチャで無理があった点

1. **`Mapping[str, Scalar]` / `tuple[Scalar, ...]` が strict converter で structure できない**
    （報告 → 裁定「案 A」で `make_strict_converter` に union hook を追加。解決済み）。
2. **group 層を root へ merge するか節へ入れるかが §13.1 に書かれていない。**
    §20 の smoke check が `from_arguments(("trainer=edge",))` → `structure(TrainerConfig)` を
    通すことから、**group 層の内容は root へ merge される**と解釈してテストを書いた。
    その結果、`SearchSpace.parameters` の dotted キーは「構造化の到達先が何か」で
    前置きが変わる（到達先 `TrainerConfig` なら `learning_rate`、節を持つ app config なら
    `trainer.learning_rate`）。`test_integration.py` の探索は前者、`test_runner.py` は後者で書いた。
    §13.4 の「キーは dotted config path（例 `trainer.learning_rate`）」は
    「到達先に対する path」と読むのが正しい、という前提。
3. **`[search_space."trainer.learning_rate"]`（§15.4）は `SearchSpace` へ直接落ちない。**
    `SearchSpace.parameters` というフィールド名が要るので、テストでは
    `[search_space.parameters."trainer.learning_rate"]` と書いた。§13.4 の TOML 例は
    1 段足りない。

## `tests/helpers.py` への追加

なし。`tests/ml/helpers.py` にも追加していない（`ml.tuning` 用のヘルパーは各テスト
ファイル内の private 関数で足りた）。**モックは 1 つも書いていない。** optuna は
in-memory study と `tmp_path` の SQLite ファイルで実物を使い、`ParameterDistribution.suggest`
には `study.ask()` で取り出した実 `Trial` を渡している。

`_RecordingObjective` は `TrialObjective`（`Callable` の type alias）の実装であって
モックではない（呼び出し引数を観測するための実オブジェクト）。

## 二重管理を防ぐための表（実装者・レビュアー向け）

`PACKAGED_GROUP_TARGETS = {"trainer": TrainerConfig}` を
`tests/ml/config/test_packaged.py` と `tests/ml/tuning/test_integration.py` の 2 箇所に
置いた。共有すると `tests/ml/helpers.py`（torch を読まない層）へ `TrainerConfig` を
持ち込むことになり、helpers の分離方針を壊すため意図的に複製している。
同梱 group を増やすときは両方へ登録する（登録漏れは
`test_every_group_has_a_registered_target` が落ちる）。

## docformatter が日本語文字を化けさせる件（実測メモ）

`make ml-docker-check` で `tests/ml/tuning/test_search_space.py` の
`実物` → `殟物`（実 U+5B9F → 殟 U+6B5F）、`内容` → `憅容`（内 U+5185 → 憅 U+61C5）が
発生した。**書き換え対象と無関係な行が化ける**ので、防御は「docformatter に書き換えさせない」
ことだけ。

- 引き金は `test_structures_categorical_choices` の summary が折り返されること。
    summary を `` ```choices`` の union hook が要る.`` まで詰めて解消した
- **折り返し判定は表示幅ではなく文字数（`len()`）**。既存の HEAD 済みファイルには
    表示幅 88 の docstring 行が残っていて docformatter は触らないが、`len()` では 78 で
    72 超。おそらく `` `` `` を含む段落を wrap 対象から外している
- **summary を小文字の識別子で始めると先頭が大文字化される。** `userinfo だけでなく...` は
    `Userinfo ...` に、`credential が...` は `Credential ...` に書き換えられた。
    `秘匿情報が...` へ直して解消（既知の落とし穴の実例）
- 検証コマンド（pre-commit と同じ pin rev `06907d0` = docformatter 1.7.5 の実体を直接叩く。
    `pre-commit run` は staged 状態に依存して unstaged を stash するため、並列作業中は使わない）

```bash
DF=/home/geson/.cache/pre-commit/repo9emx_wox/py_env-python3/bin/docformatter
docker compose -f docker/compose.yaml exec -T ml bash -c \
  "cd /workspace && $DF --check --diff --wrap-summaries=79 --wrap-descriptions=72 \
   \$(find tests/ml -name '*.py' | sort | tr '\n' ' ')"
```

`tests/ml` 全 42 file で **exit 0（書き換えゼロ）**。`殟` / `憅` の混入も
`grep -rn` で無いことを確認した。

## 記録された決定

- **group 層は root へ merge する**（節へ入れない）。計画 §13.1 に明記が無かったが、
    §20 の smoke check（`trainer=edge` → `structure(TrainerConfig)`）と整合する解釈が採用された。
    その結果、`SearchSpace.parameters` の dotted キーは「構造化の到達先に対する path」であり、
    到達先が `TrainerConfig` なら `learning_rate`、節を持つ app config なら `trainer.learning_rate`
- `[search_space.parameters."trainer.learning_rate"]` が正しい階層（計画 §15.4 の TOML 例は
    1 段足りなかった）
- **裁定 1**: CLI 上書きは裸の文字列を `str` として受ける。型の正しさを守るのは合成層ではなく
    strict converter（決定 3 の `learning_rate=1` 拒否は維持）
- **裁定 5**: `isinstance(loaded, dict)` は実装から削除

## orchestrator へ差し戻したい設計上の疑問

前回挙げた 3 件（group 層の merge 先 / #29 の検出力 / #4 の扱い）はいずれも裁定済み。
新たな差し戻しは無い。

残る注意点は 1 つだけ:

1. **docformatter の文字化けは根本原因が不明のまま**（上記メモ）。「書き換えさせない」
    運用で回避しているので、MR6 以降で docstring を足すときも
    `--check` が exit 0 であることを確認してから commit する必要がある。
    `tests/ml` 以外（`src/ml`）でも同じ危険があるので、実装側にも共有したい。

## ユーザーへの質問

なし。
