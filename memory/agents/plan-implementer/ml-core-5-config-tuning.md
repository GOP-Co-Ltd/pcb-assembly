# MR5 設定合成 + ハイパーパラメータ探索（実装）

計画書: `memory/agents/implementation-planner/ml-core-5-config-tuning.md`（§13 のシグネチャは変更なしで実装した）

## 計画外の判断ログ

### 1. `ml/serialization.py` へ Scalar union の structure hook を追加（orchestrator 裁定で追加）

当初「一切変更しない」指示だったが、実測で `type Scalar = bool | int | float | str` が
cattrs で構造化できないことが判明（unstructure は通る）。`TrialRecord.parameters` と
`ParameterDistribution.choices` の両方が詰まる。orchestrator の裁定通達どおり
`_SCALAR_TYPES` を module 定数へ括り出し、同じ 1 本から
`register_structure_hook(bool | int | float | str, _structure_scalar_union)` を登録した。

- `isinstance(...) and type(value) in _SCALAR_TYPES` の併記は pyright の narrowing と
    部分型（`IntEnum`）拒否の両方に必要。片方だけにはできない
- 決定 3（int→float 昇格を入れない・`_exact_type` を削らない）は維持。実測でも
    `learning_rate=1` は `expected float` で拒否されたまま

### 2. `from_arguments` は `validate()` を呼ばない

一度は末尾で `validate()` を呼ぶ実装にしたが、§13.1 が挙げる異常系は
「group が実在するが option の TOML が無い」だけなので外した。`base_names` の層が
実在しない場合は `compose()`（先頭で `validate()` を呼ぶ）が理由を返す。

### 3. group 層は group 名の下へ nest せず、そのまま最上位へ merge する

§20 が `from_arguments(("trainer=edge",), ...)` → `structure(TrainerConfig, ...)` と
書いているため、group 選択は「`conf/trainer/edge.toml` を層として積む」だけとし、
`{"trainer": {...}}` へ包まない。`conf/trainer/edge.toml` も TrainerConfig の
フィールドを最上位に持つ。

### 4. group / 上書きの判定はディレクトリ実在のみ（dotted 判定を足さない）

§16 #9 の意図どおり `root / name` が `is_dir()` かだけで振り分ける。
`.` を含むかどうかは見ない。

### 5. `sqlite:///` の絶対パス判定は SQLAlchemy の規則に合わせた

`urlsplit(uri).path` から先頭 1 個の `/` を落としたものを database path として扱う。
SQLAlchemy は `sqlite:///foo.db` を相対、`sqlite:////abs/foo.db` を絶対と読むため。
実測: `f"sqlite:///{tmp_path / 'study.db'}"`（= 4 連 slash）は通り、
`sqlite:///relative.db` は理由を返す。

### 6. `redacted_uri` は netloc の userinfo だけを落とす

`urlsplit(...).port` は不正な port で `ValueError` を投げるため、`netloc.rpartition("@")`
で userinfo を捨てる実装にした（port は文字列のまま残るので自然に保たれる)。
**query string 中の credential（`?password=...`）は落としていない。** 現状 optuna の
storage URI で使っていないため対象外にした。必要なら別タスク。

### 7. orchestrator 裁定による 5 件の修正（第 2 ラウンド）

確認事項 4 件と `spec-test-author` 報告 1 件に裁定が出たので反映した。

1. **CLI 上書きは裸の文字列を受け付ける。** `tomllib.loads(f"value = {raw}")` を先に
    試し、`TOMLDecodeError` なら raw をそのまま `str` として採用する。shell が
    クォートを食うので `monitor=mae` / `mode=max` を TOML 文字列として渡す手段が
    実質無いため。型の正しさは strict converter が守る（実測: `max_epochs=abc` は
    「int が必要です」、`learning_rate=1` は「float が必要です」で拒否。**決定 3 は保たれる**）。
    `_parse_override_value` は失敗しなくなったので戻り値を `object` へ簡素化した
2. **`HyperparameterSearch.validate` が fingerprint の一致を検査する。**
    `identity.search_space_fingerprint != search_space.fingerprint` なら理由を返す。
    study 名が空間の fingerprint を含むので、食い違うと空間の違う trial が
    1 個の study へ合流する
3. **`redacted_uri` は userinfo に加えて query と fragment も落とす。** 実装は
    `urlunsplit` ではなく文字列操作にした。

    `urlunsplit` を避ける判断は妥当だが、当初この項に書いた根拠は誤りだった
    ので orchestrator が訂正した。`sqlite:////abs.db` は netloc 空でも
    `path` が `//` で始まるため `urlunsplit` が `//` を復元し、**壊れない**。

    実際に壊れるのは `path` が `//` で始まらない場合。`sqlite:///relative.db`
    → `sqlite:/relative.db`、`sqlite://` → `sqlite:` になる（いずれも実測）。
    `sqlite` / `postgresql` が `urllib.parse.uses_netloc` に無いという事実自体は
    正しい。前者は `validate()` が絶対パスを要求して弾くので実害は無いが、
    文字列操作にした結論は変えない
4. **`integer` は非整数の `low` / `high` を拒否する。** `int()` で黙って切ると
    記録した範囲と実際に探した範囲が食い違う
5. **`_load_layer` の `isinstance(loaded, dict)` を削除。** `tomllib` の最上位は常に
    mapping なので公開経路から到達できない。**pyright は 0 errors のまま**
    （`tomllib.load` の戻り値が `dict[str, Any]` なので narrowing 不要）。
    §16 #4 の変異は対象が消えるため、この行は対応表から落とす必要がある

### 8. group 層は root へ merge する（計画に無かった決定・要記録）

group 選択は「`conf/<group>/<option>.toml` を層として積む」だけで、`{group: {...}}` の
節へは入れない。§20 が `from_arguments(("trainer=edge",), ...)` →
`structure(TrainerConfig, ...)` と書いているため。`spec-test-author` も同じ解釈で緑。

- 結果として、dotted キーの前置きは構造化の到達先によって変わる
- `SearchSpace` を TOML から読む場合の正しい階層は
    `[search_space.parameters."trainer.learning_rate"]`（計画 §15.4 の記述は 1 段
    足りなかった）。`ConfigComposition(layer_paths=...).structure(SearchSpace, ...)`
    で層の最上位を `SearchSpace` に落とすなら `[parameters."trainer.learning_rate"]`

### 9. docformatter に書き換えさせない

orchestrator 通達の文字化けバグ（`実`→`殟`、`内`→`憅`）は「書き換えが起きなければ
発生しない」ため、担当ファイルは **docformatter が `--diff` で何も出さない状態**へ
揃えた。docstring の段落は 1 文 1 行に収め、折り返しが起きる長さを避ける。

第 1 ラウンドで実際に 2 箇所（`StudyIdentity` の class docstring、
`TrialAssignment.as_override_arguments`）が連結崩れを起こしていたので直した。
文字化けの混入は grep で無しを確認（`殟` / `憅` 不在、使用 CJK 307 字を目視確認）。

## 他 implementer への IF 変更通知（並列時）

§13 のシグネチャは 1 つも変えていない。`spec-test-author` 向けの追加情報のみ:

- 公開した追加の module 定数: `ml.tuning.study.DIRECTIONS`、
    `ml.tuning.search_space.DISTRIBUTION_KINDS`（どちらも tuple。`__all__` に含む）
- `ml.config.composition.ConfigMapping`（`type ConfigMapping = Mapping[str, object]`）も
    `__all__` に含む

## 既知の制約・残課題

確認事項 1〜4 はすべて裁定が出て修正済み（上記 7）。残るのは次の 2 件。

1. **`a=` のような空の値は空文字列になる。** 裁定 1 の fallback により、TOML として
   解釈できない token は raw 文字列として採用されるため。型が合わなければ strict
   converter が拒否する
2. `conf/trainer/edge.toml` は `max_epochs` / `monitor` を持つ。TrainerConfig の
   必須フィールドなので、単層で `structure(TrainerConfig)` を通すには必要
   （既定値を持たないフィールドなので「既定値の二重管理」には当たらない）
3. `ml_smoke.py` の `check_packaged_configuration` は `ml.training.loop` 経由で torch を
   読む。smoke は既に torch を読んでいるので追加コストのみ

## 検証結果

`make ml-docker-check` は使っていない（並列で動く `spec-test-author` と `.git` ロックで
競合するため、orchestrator の指示どおり回避）。個別に実施した。

- 型検査: `uv run pyright src/ml tests/ml scripts/ml_smoke.py` → **0 errors, 0 warnings**
    （裁定 5 で防御分岐を削ったあとも 0 errors）
- テスト: `uv run pytest tests/ml -m "not hardware and not e2e"` →
    **756 passed / 1 skipped**（ベースライン 592 + `spec-test-author` の新規分。failed 0）
- smoke: `uv run python scripts/ml_smoke.py` → **すべて通過**
    （`version hydra-core` の行が消え、`packaged configuration  1 group（trainer=edge）` が出る）
- format: pre-commit 全体は回していない。代わりに pre-commit のキャッシュ済み hook
    実体（ruff v0.8.4 / docformatter / codespell）を自分の変更ファイルだけに直接適用した。
    `ruff check` / `ruff format --check` / codespell はいずれも clean で、
    **docformatter は `--diff` で何も出さない**（書き換えさせないのが文字化けバグへの
    唯一の防御）。
    **合流時に `git add -A && make ml-docker-check` を 1 度通すこと**（未追跡ファイルは
    pre-commit の対象にならないため）

## 変異実験

対応表は `memory/agents/spec-test-author/ml-core-5-config-tuning.md` §「テスト ↔ §16 の
対応行 ↔ 潰す機構」（#4 を除く 46 行 + 追加行 #48〜#59）。1 行が複数の分岐を指す場合は
分岐ごとに枝番（`15a` / `27a` など）へ割り、**合計 73 変異**を実測した。

### 手順と安全機構

index を正本にした（全ファイルが `git add` 済み）。ドライバ
（`/tmp/.../driver.py`、セッション限りの scratchpad）が変異ごとに

1. 完全一致の文字列置換で 1 変異だけ当てる（一致数が 1 でなければ中止）
2. `docker compose exec -T ml uv run pytest tests/ml -m "not hardware and not e2e"` を全件実行
3. `try/finally` + `atexit` で `git restore --worktree <file>` して戻す
4. `git diff --stat` が空であることを確認する

を回した。`git add` / `commit` / `stash` / `checkout` / `restore --staged` は 1 度も
使っていない。全 73 変異で残骸ゼロ（テスト補強を入れた後は、その差分だけが残る）。

変異ごとに全件（763 件・約 27 秒）を回した。対象テストだけに絞るより、
「対応表が想定していないテストが拾っているか」まで見えるのを優先した。

### 実測結果

- **73 変異中 68 が初回で KILLED、5 が SURVIVED。**
- SURVIVED 5 件はすべてテストを補強して KILLED に転じさせた（下記）。
- ベースライン 756 passed / 1 skipped → 補強後 **763 passed / 1 skipped**。

| # | 変異 | 結果 | 落ちたテスト |
| --- | --- | --- | --- |
| 1 | _merge_into の再帰 merge を素の dict.update にする | **KILLED**（1 件） | `TestComposeLayers::test_merges_later_layers_over_earlier_ones_recursively` |
| 2 | _merge_into が list を連結する | **KILLED**（1 件） | `TestComposeLayers::test_replaces_lists_instead_of_concatenating_them` |
| 3 | _load_layer の tomllib.TOMLDecodeError 捕捉を外す | **KILLED**（1 件） | `TestComposeLayers::test_reports_a_malformed_toml_layer` |
| 5 | _assign の中間 table 生成を削る | **KILLED**（7 件） | `TestApplyOverrides::test_accepts_a_bare_string_without_quotes`<br>`TestApplyOverrides::test_creates_missing_intermediate_tables`<br>`TestStructure::test_accepts_a_bare_string_for_a_literal_field`<br>`…他 4 件` |
| 6 | _assign の isinstance(child, dict) 判定を削る | **KILLED**（1 件） | `TestApplyOverrides::test_reports_an_intermediate_key_that_is_not_a_table` |
| 7 | _parse_override_value の TOML 解釈をやめて全部 str にする | **KILLED**（12 件） | `TestApplyOverrides::test_creates_missing_intermediate_tables`<br>`TestApplyOverrides::test_interprets_values_with_the_toml_scalar_rules[value="x"-x]`<br>`TestApplyOverrides::test_interprets_values_with_the_toml_scalar_rules[value=1.0e-4-0.0001]`<br>`…他 9 件` |
| 8 | ConfigComposition.validate の `if not separator or not key` を削る | **KILLED**（1 件） | `TestApplyOverrides::test_reports_a_token_without_a_separator` |
| 9 | group 判定をディレクトリ実在から「. を含むか」に変える | **KILLED**（2 件） | `TestFromArguments::test_treats_a_token_whose_name_is_not_a_directory_as_an_override`<br>`TestComposeThenSearch::test_runs_a_search_over_the_packaged_configuration` |
| 10 | 層の連結順を group → base へ入れ替える | **KILLED**（1 件） | `TestFromArguments::test_stacks_base_layers_before_group_layers` |
| 11 | ConfigComposition.structure が structure_strictly ではなく素の converter.structure を呼ぶ | **KILLED**（5 件） | `TestStructure::test_rejects_a_bare_string_for_an_integer_field`<br>`TestStructure::test_rejects_a_string_outside_a_literal_field`<br>`TestStructure::test_rejects_an_integer_for_a_float_field`<br>`…他 2 件` |
| 12 | strict converter に int → float 昇格 hook を足す（決定 3 の破壊） | **KILLED**（3 件） | `TestStructure::test_rejects_an_integer_for_a_float_field`<br>`TestStrictScalars::test_rejects_implicitly_convertible_values[ratio-0.5]`<br>`TestStrictScalars::test_rejects_implicitly_convertible_values[ratio-1]` |
| 13 | PackagedConfiguration.locate のパス計算を 1 段ずらす | **KILLED**（8 件） | `TestPackagedConfiguration::test_lists_the_edge_option_of_the_trainer_group`<br>`TestPackagedConfiguration::test_lists_the_trainer_group`<br>`TestPackagedConfiguration::test_locates_an_existing_directory`<br>`…他 5 件` |
| 14 | conf/trainer/edge.toml を削除する | **KILLED**（4 件） | `TestPackagedConfiguration::test_lists_the_edge_option_of_the_trainer_group`<br>`TestPackagedOptionsStructure::test_every_packaged_option_structures_strictly`<br>`TestComposeThenSearch::test_runs_a_search_over_the_packaged_configuration`<br>`…他 1 件` |
| 15a | study_name の組み立てから model_family を落とす | **KILLED**（3 件） | `TestCollect::test_reports_a_study_that_does_not_exist`<br>`TestStudyIdentity::test_changes_the_name_when_any_element_changes[other-family-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]`<br>`TestStudyIdentity::test_sanitizes_the_model_family_and_truncates_the_fingerprints` |
| 15b | study_name の組み立てから dataset_fingerprint を落とす | **KILLED**（2 件） | `TestStudyIdentity::test_changes_the_name_when_any_element_changes[gaussian-regressor-sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc-sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]`<br>`TestStudyIdentity::test_sanitizes_the_model_family_and_truncates_the_fingerprints` |
| 15c | study_name の組み立てから search_space_fingerprint を落とす | **KILLED**（2 件） | `TestStudyIdentity::test_changes_the_name_when_any_element_changes[gaussian-regressor-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd]`<br>`TestStudyIdentity::test_sanitizes_the_model_family_and_truncates_the_fingerprints` |
| 16a | _safe_name の re.sub を削る | **KILLED**（3 件） | `TestStudyIdentity::test_reports_a_model_family_without_usable_characters[   ]`<br>`TestStudyIdentity::test_reports_a_model_family_without_usable_characters[///]`<br>`TestStudyIdentity::test_sanitizes_the_model_family_and_truncates_the_fingerprints` |
| 16b | fingerprint の切り出し桁数を 12 → 8 に変える | **KILLED**（1 件） | `TestStudyIdentity::test_sanitizes_the_model_family_and_truncates_the_fingerprints` |
| 17 | sqlite storage の is_absolute() 判定を削る | **KILLED**（3 件） | `TestHyperparameterSearchRun::test_does_not_create_a_study_when_validation_fails[storage]`<br>`TestHyperparameterSearchValidation::test_reports_storage_that_cannot_be_shared`<br>`TestStudyStorage::test_reports_storage_that_cannot_be_shared[sqlite:///study.db]` |
| 18a | sqlite の in-memory（database 名なし）判定を削る | SURVIVED → 補強後 **KILLED**（1 件） | `TestStudyStorage::test_separates_in_memory_sqlite_from_a_relative_path` |
| 18b | server storage の host 必須判定を削る | **KILLED**（1 件） | `TestStudyStorage::test_reports_storage_that_cannot_be_shared[postgresql:///db]` |
| 18c | server storage の database 名必須判定を削る | **KILLED**（1 件） | `TestStudyStorage::test_reports_storage_that_cannot_be_shared[postgresql://host]` |
| 18d | scheme 許可リストを外し、未対応 scheme を通す | **KILLED**（1 件） | `TestStudyStorage::test_reports_storage_that_cannot_be_shared[redis://host/0]` |
| 19 | redacted_uri が self.uri をそのまま返す | **KILLED**（7 件） | `TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://db.example:5433/hpo#secret]`<br>`TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://db.example:5433/hpo?password=secret]`<br>`TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://operator:secret@db.example:5433/hpo]`<br>`…他 4 件` |
| 20 | redacted_uri の port 連結を落とす | **KILLED**（4 件） | `TestStudyStorage::test_drops_every_place_a_credential_can_hide[postgresql://db.example:5433/hpo#secret]`<br>`TestStudyStorage::test_drops_every_place_a_credential_can_hide[postgresql://db.example:5433/hpo?password=secret]`<br>`TestStudyStorage::test_drops_every_place_a_credential_can_hide[postgresql://operator:secret@db.example:5433/hpo]`<br>`…他 1 件` |
| 58 | redacted_uri の query / fragment 除去を落とす（userinfo だけ落とす実装へ戻す） | **KILLED**（4 件） | `TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://db.example:5433/hpo#secret]`<br>`TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://db.example:5433/hpo?password=secret]`<br>`TestStudyStorage::test_drops_every_place_a_credential_can_hide[postgresql://db.example:5433/hpo#secret]`<br>`…他 1 件` |
| 21 | best_trial を常に min にする | **KILLED**（1 件） | `TestStudyResults::test_selects_the_best_trial_by_direction[maximize-0]` |
| 22 | best_trial の is_complete 絞り込みを削る | **KILLED**（1 件） | `TestStudyResults::test_does_not_consider_incomplete_trials_as_best` |
| 23 | StudyResults に生 storage_uri フィールドを足し build で埋める | **KILLED**（3 件） | `TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://db.example:5433/hpo#secret]`<br>`TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://db.example:5433/hpo?password=secret]`<br>`TestStudyResults::test_never_writes_a_credential_into_the_document[postgresql://operator:secret@db.example:5433/hpo]` |
| 24 | DocumentKind.load の schema_version 照合を削る | **KILLED**（2 件） | `TestDocumentKindStructure::test_reports_a_mismatched_schema_version`<br>`TestStudyResults::test_reports_an_unsupported_schema_version` |
| 25 | verify_lineage を常に None を返すようにする | **KILLED**（1 件） | `TestVerifyLineage::test_reports_the_numbers_of_completed_trials_without_a_run_id` |
| 26 | verify_lineage の絞り込みを外し全 trial に run id を要求する | **KILLED**（1 件） | `TestVerifyLineage::test_allows_a_missing_run_id_on_failed_or_pruned_trials` |
| 27a | kind の許可リスト判定を削る | SURVIVED → 補強後 **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution19]` |
| 27b | categorical の choices 必須判定を削る | **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution11]` |
| 27c | categorical の low / high 禁止判定を削る | **KILLED**（2 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution12]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution13]` |
| 27d | categorical の log 禁止判定を削る | **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution14]` |
| 27e | categorical の step 禁止判定を削る | **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution15]` |
| 27f | numeric の choices 禁止判定を削る | **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution5]` |
| 27g | numeric の low / high 必須判定を削る | **KILLED**（4 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution0]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution1]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution8]`<br>`…他 1 件` |
| 27h | numeric の有限値判定を削る | SURVIVED → 補強後 **KILLED**（2 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution4]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution5]` |
| 27i | numeric の low < high 判定を削る | **KILLED**（2 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution2]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution3]` |
| 27j | log スケールの正の low 判定を削る | **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution4]` |
| 27k | float の step 禁止判定を削る | SURVIVED → 補強後 **KILLED**（1 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution8]` |
| 27l | step >= 1 判定を削る | **KILLED**（2 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution6]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution7]` |
| 59 | kind=integer の非整数境界を拒否せず int() で黙って切り捨てる（裁定 4） | **KILLED**（2 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution10]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution9]` |
| 28 | suggest_int へ step を渡すのを止める | **KILLED**（1 件） | `TestSuggest::test_suggests_an_integer_on_the_step_grid` |
| 29 | suggest_float へ log を渡すのを止める | SURVIVED → 補強後 **KILLED**（1 件） | `TestSuggest::test_registers_the_logarithmic_scale_with_optuna[True]` |
| 30 | SearchSpace.fingerprint を sort_keys=False の json.dumps にする | **KILLED**（1 件） | `TestSearchSpaceFingerprint::test_ignores_the_insertion_order_of_the_parameters` |
| 31 | SearchSpace.fingerprint の対象から log を落とす | **KILLED**（1 件） | `TestSearchSpaceFingerprint::test_changes_when_the_logarithmic_flag_changes` |
| 32 | TrialAssignment.as_tags の hpo. 前置きを外す | **KILLED**（1 件） | `TestTrialAssignment::test_prefixes_every_tag_with_the_search_namespace` |
| 33 | as_override_arguments の区切りを = から : に変える | **KILLED**（3 件） | `TestComposeThenSearch::test_runs_a_search_over_the_packaged_configuration`<br>`TestSearchedValuesDoNotEnterParams::test_the_searched_value_itself_is_recorded_as_a_param`<br>`TestTrialAssignment::test_produces_override_arguments_that_the_composition_accepts` |
| 34 | create_study の load_if_exists=True を外す | **KILLED**（2 件） | `TestHyperparameterSearchRun::test_joins_trials_from_two_instances_sharing_one_storage`<br>`TestHyperparameterSearchRun::test_resumes_an_existing_study_without_reducing_the_trial_count` |
| 35 | trial_count を残 trial 数へ減算する（MR185 の再現） | **KILLED**（2 件） | `TestHyperparameterSearchRun::test_joins_trials_from_two_instances_sharing_one_storage`<br>`TestHyperparameterSearchRun::test_resumes_an_existing_study_without_reducing_the_trial_count` |
| 36 | run 先頭の validate() 呼び出しを削る | **KILLED**（3 件） | `TestHyperparameterSearchRun::test_does_not_create_a_study_when_validation_fails[search_space]`<br>`TestHyperparameterSearchRun::test_does_not_create_a_study_when_validation_fails[storage]`<br>`TestHyperparameterSearchRun::test_does_not_create_a_study_when_validation_fails[trial_count]` |
| 37 | study.optimize の catch=(Exception,) を外す | **KILLED**（1 件） | `TestHyperparameterSearchRun::test_records_a_failing_trial_without_failing_the_whole_search` |
| 38 | collect が study.optimize を呼ぶようにする | **KILLED**（4 件） | `TestComposeThenSearch::test_saves_a_document_whose_lineage_verifies`<br>`TestCollect::test_fills_the_experiment_run_ids_that_are_given`<br>`TestCollect::test_leaves_the_experiment_run_ids_empty_by_default`<br>`…他 1 件` |
| 57 | HyperparameterSearch.validate の fingerprint 一致検査を削る（裁定 2） | **KILLED**（1 件） | `TestHyperparameterSearchValidation::test_reports_a_search_space_that_does_not_match_the_identity` |
| 39 | conf/trainer/edge.toml に attrs 既定値と同じ行（seed = 0）を 1 行足す | **KILLED**（1 件） | `TestDefaultsAreNotDuplicated::test_no_packaged_option_repeats_an_attrs_default` |
| 40 | _FINGERPRINT_EXCLUDED_FIELDS から deadline_seconds を削る | **KILLED**（6 件） | `TestTrainerConfig::test_time_budget_fields_do_not_change_the_fingerprint[deadline_seconds]`<br>`TestTrainerFullRun::test_time_budget_fields_are_tags_not_params`<br>`TestTrainerResumeWithANewTimeBudget::test_resume_with_a_different_deadline_is_recorded`<br>`…他 3 件` |
| 41 | TrainerConfig.fingerprint を定数にする | **KILLED**（20 件） | `TestTrainerConfig::test_fingerprint_is_content_addressed`<br>`TestTrainerConfig::test_semantic_fields_change_the_fingerprint[automatic_mixed_precision_enabled]`<br>`TestTrainerConfig::test_semantic_fields_change_the_fingerprint[compile_enabled]`<br>`…他 17 件` |
| 42a | TrainerConfig.as_tags を空 dict にする | **KILLED**（3 件） | `TestTrainerFullRun::test_time_budget_fields_are_tags_not_params`<br>`TestTrainerResumeWithANewTimeBudget::test_resume_with_a_different_deadline_is_recorded`<br>`TestFingerprintClassificationSurvivesComposition::test_the_time_budget_reaches_the_tags_with_the_training_prefix` |
| 42b | TrainerConfig.as_params の時間予算除外を外す | **KILLED**（3 件） | `TestTrainerFullRun::test_time_budget_fields_are_tags_not_params`<br>`TestTrainerResumeWithANewTimeBudget::test_resume_with_a_different_deadline_is_recorded`<br>`TestFingerprintClassificationSurvivesComposition::test_the_time_budget_never_reaches_the_params` |
| 43 | 探索メタデータ（hpo.study_name）を param 側へ合流させる | **KILLED**（1 件） | `TestSearchedValuesDoNotEnterParams::test_search_metadata_never_shares_a_key_with_the_params` |
| 45 | ml/tuning/study.py に import optuna を足す | **KILLED**（1 件） | `TestDependencyFreeLayer::test_importing_them_does_not_load_heavy_dependencies` |
| 46 | ml/training/loop.py に import optuna を足す | **KILLED**（1 件） | `TestRuntimeLayer::test_importing_them_does_not_load_training_only_dependencies` |
| 47 | ml/config/composition.py に from pcbasm.config import Machine を足す | **KILLED**（1 件） | `TestDomainIndependence::test_no_module_imports_a_domain_package` |
| 48 | make_strict_converter の Scalar union structure hook 登録を削る | **KILLED**（13 件） | `TestScalarUnion::test_keeps_the_exact_scalar_type[count-3]`<br>`TestScalarUnion::test_keeps_the_exact_scalar_type[flag-True]`<br>`TestScalarUnion::test_keeps_the_exact_scalar_type[name-base]`<br>`…他 10 件` |
| 49 | _structure_scalar_union の type(value) in _SCALAR_TYPES を消す | **KILLED**（1 件） | `TestScalarUnion::test_rejects_a_subtype_of_int` |
| 50 | Scalar union hook が値を float へ正規化する | **KILLED**（3 件） | `TestScalarUnion::test_keeps_the_exact_scalar_type[count-3]`<br>`TestScalarUnion::test_keeps_the_exact_scalar_type[flag-True]`<br>`TestScalarUnion::test_round_trips_without_promoting_integers_to_floats` |
| 51 | Scalar union hook の raise を削り値をそのまま返す | **KILLED**（4 件） | `TestScalarUnion::test_rejects_a_subtype_of_int`<br>`TestScalarUnion::test_rejects_values_that_are_not_scalars[None]`<br>`TestScalarUnion::test_rejects_values_that_are_not_scalars[wrong_value0]`<br>`…他 1 件` |
| 53 | 同梱 group（optimizer）を増やし PACKAGED_GROUP_TARGETS へ登録しない | **KILLED**（1 件） | `TestPackagedOptionsStructure::test_every_group_has_a_registered_target` |
| 54 | 裸の文字列を TOML として解釈できないと拒否する実装へ戻す（裁定 1 の回帰） | **KILLED**（6 件） | `TestApplyOverrides::test_accepts_a_bare_string_without_quotes`<br>`TestApplyOverrides::test_interprets_values_with_the_toml_scalar_rules[value=mae-mae]`<br>`TestApplyOverrides::test_interprets_values_with_the_toml_scalar_rules[value=max-max]`<br>`…他 3 件` |
| 56 | strict converter の Literal 検証を外す | **KILLED**（2 件） | `TestStructure::test_rejects_a_string_outside_a_literal_field`<br>`TestStrictShapes::test_rejects_values_outside_the_declared_shape[mode-export]` |

### SURVIVED だった 5 件（テストが機構を守っていなかった箇所）

いずれも「拒否そのものは別の分岐が代替するので、分岐を消しても観測結果が変わらない」
か「観測点が弱すぎる」のどちらかだった。

1. **#18a `sqlite://`（in-memory）判定** — 消しても直後の `is_absolute()` 判定が
    拾うので `validate()` は理由を返し続ける。`test_reports_storage_that_cannot_be_shared`
    は `is not None` しか見ていないため気付けない。
    補強: `TestStudyStorage::test_separates_in_memory_sqlite_from_a_relative_path` を追加し、
    `sqlite://` は「in-memory」、`sqlite:///study.db` は「絶対パス」と**理由を区別**して
    報告することを固定した。
2. **#27a `kind` 許可リスト判定** — 未知 `kind` のケースが 1 つも無かった。
    補強: `ParameterDistribution(kind=cast("DistributionKind", "unknown"), low=1.0, high=2.0)`
    を追加。`low` / `high` を**埋めた**値で書くのが要点で、省くと許可リストを消しても
    「low と high が必要です」が拾ってしまい変異が生き残る（実測済み）。
3. **#27h 有限値（`math.isfinite`）判定** — `inf` / `nan` のケースが無かった。
    補強: `kind="float", high=float("inf")` と `low=float("nan")` の 2 ケースを追加。
4. **#27k `float` に `step` を書けない判定** — `step` の異常系が `kind="integer"` の
    `step=0` / `step=-1` だけで、これは別分岐（`step < 1`）が拾う。
    補強: `kind="float", low=1.0e-5, high=1.0e-2, step=2` を追加。
5. **#29 `suggest_float` への `log` 受け渡し** — 対応表が「検出力は弱い」と自認していた行。
    範囲チェックだけでは `log` を落としても偶然範囲内に入る。
    補強: `TestSuggest::test_registers_the_logarithmic_scale_with_optuna[False/True]` を追加。
    実 `Trial` の `trial.distributions[name]` が `FloatDistribution(log=...)` を
    記録していることを見る（optuna の実オブジェクトの観測。モックではない）。
    これで §16 #29 の「二重に持たない」制約を崩さずに検出力を得た。

### 補強しなかった弱点（記録のみ）

- **#8 の 2 テストのうち `test_reports_a_token_with_an_empty_key` は落ちなかった。**
    `if not separator or not key` を消しても `any(not part for part in key.split("."))`
    が `"=1"` を拾う。行としては `test_reports_a_token_without_a_separator` が
    KILLED しており、「不正な上書き token を拒否する」機構自体は守られているので
    追加テストは書かない（同じ入力に 2 本の検出器を持たせるだけになる）。
- **#43 `test_search_metadata_never_shares_a_key_with_the_params`** は
    `TrainerConfig.as_params()` へ `hpo.study_name` を混ぜる変異で KILLED した。
    ただし `_TAG_PREFIX = ""`（#32）では落ちない（キーが衝突しないため）。
    名前空間そのものは #32 の `test_prefixes_every_tag_with_the_search_namespace` が
    `set(tags) == {"hpo.study_name", "hpo.trial_number"}` で固定しているので、
    役割分担として妥当と判断した。

### 副次的に分かったこと

- **#11 / #55 は同一変異（`structure_strictly` → 素の `converter.structure`）で
    5 テストが落ちる。** `_exact_type` hook は converter 側に登録されているので
    型の厳格さ自体は保たれ、落ちるのは「例外ではなく理由文字列を返す」契約の方。
- **#48（Scalar union hook 削除）は 13 テストを落とす。** #52（categorical の TOML）と
    #24（`StudyResults` の save→load）が同じ 1 行に依存していることが実測で確認できた。
- **#24 は `DocumentKind.load` の `schema_version` 照合削除で実測した。**
    対応表の「`DOCUMENT_KIND.schema_version` を上げて `load` 側だけ据え置く」は
    save / load が同じ `DocumentKind` を共有するので往復が成立してしまい、変異にならない。
- **#17 / #36 の変異中、`sqlite:///relative.db` が拒否されなくなる結果、リポジトリ直下に
    `relative.db` が作られた。** 変異戻し後に削除済み（`git status` に untracked なし）。
    テスト側が相対パスの storage を実ファイルとして作りうる点は、実装が
    `is_absolute()` で拒否していることに依存している。
- **#47（`from pcbasm.config import Machine` 追加）だけは `tests/ml` 全件ではなく
    `tests/ml/test_architecture.py` に絞って実測した。** 学習機には picamera2 が無く、
    `ml.config.composition` が import 不能になって全件が collect error になるため。

### 検証（変異実験後）

`make ml-docker-check` は使っていない。`pre-commit run` は unstaged を stash するため、
index を正本にする今回の安全手順と両立しない。pin rev と同一の hook 実体を直接叩いた。

- `uv run pyright src/ml tests/ml` → **0 errors, 0 warnings**
- ruff 0.8.4 `check` / `format --check` → clean
- docformatter 1.7.5（pin rev `06907d0`）`--check --diff --wrap-summaries=79
    --wrap-descriptions=72` → **exit 0（書き換えゼロ）**
- codespell v2.3.0 / pyupgrade `--py312-plus` → exit 0
- `grep -rn "殟\|憅" src/ml tests/ml` → **該当なし**
- `tests/ml -m "not hardware and not e2e"` → **763 passed / 1 skipped**

## code-reviewer 差し戻し対応（must-fix 3 / should-fix 4）

レビュー記録: `memory/agents/code-reviewer/ml-core-5-config-tuning.md`（verdict: request-changes）。
orchestrator が must-fix 3 件を独立に再現したうえで裁定を出した。**このラウンドは `src/` と
`tests/` の両方を担当した。**

### must-fix 1: `redacted_uri` の秘匿漏れ（`ml/tuning/study.py`）

2 経路とも塞いだ。**`urlsplit` をこの class から完全に外した。**

- 実測で `urlsplit("postgresql://operator:secret@[::1/hpo")` は
    `ValueError: Invalid IPv6 URL` を投げる。つまり従来の `validate()` は**理由文字列を
    返す約束を破って例外を投げる**経路を持っていた（レビューは秘匿漏れとして
    指摘していたが、実体はそれより広い）
- 代わりに `_split_uri(uri) -> (scheme, host, path)` を置いた。query / fragment を落とし、
    `scheme://` の形に読めなければ scheme を空で返す。scheme は
    `[a-zA-Z][a-zA-Z0-9+.-]*` の fullmatch で判定するので、`operator:secret@host://db` の
    ように userinfo が scheme の位置へ来る形も弾く（`urlsplit` は `operator` を
    scheme として受けるため、この形は `未対応の scheme です: 'operator'` で
    **username を漏らしていた**）
- 解釈できない URI は生の文字列を 1 文字も返さず `<解釈できない storage URI>` を返す
- 組み立て後に `"@" in redacted` を検査する。userinfo に生の `/` が混じった
    `postgresql://operator:sec/secret@db.example/hpo` は authority と path の境界が
    決められないので、この 1 行が最後の砦になる
- **理由文字列の `{self.uri!r}` を全廃した。** sqlite 分岐 2 箇所が生 URI を埋めていた

### must-fix 2: `integer` + `log=True` + `step > 1` と、探索全滅の silent success

- `_validate_numeric` に `log and step is not None and step != 1` の分岐を足した。
    optuna は実測でこの組み合わせを常に拒否する（`only accept step is 1 when log is True`）
- `run` は `completed_trial_count == 0` なら理由を返す。理由に要求 trial 数と
    `_state_breakdown`（`FAIL=3` のような state 内訳）を載せ、診断情報を残す
- `validate()` の `trial_count >= 1` は `run` 先頭で必ず通るので、
    追加のガードは `completed_trial_count == 0` の 1 本だけにした

### must-fix 3: 既存 study との `direction` 不一致（裁定どおり `run` で拒否）

`create_study(load_if_exists=True)` の直後に `study.direction.name.lower()` と
`self.direction` を照合し、不一致なら trial を積まずに理由を返す。`StudyIdentity` は
変えていない（§13 の公開シグネチャと study 名の形を保つ）。MR4 の
`TrainingCheckpoint.resume_rejection()` と同じ「永続化された状態が現在の設定と
合わなければ理由を返す」型に揃えた。

### should-fix (a): objective の非有限値

`_evaluate` が `math.isfinite` を検査し、非有限なら `NonFiniteTrialValueError`
（`RuntimeError` 派生。MR4 の `NonFiniteLossError` と同じ形）を投げる。optuna の
`catch=(Exception,)` がこれを受けて trial を FAILED として記録するので、`inf` は
成果物へ載らない。**例外を投げるのは optuna へ「この trial は失敗」と伝える手段**で
あって、検証失敗を例外で返しているのではない旨を docstring に書いた。

**既知の制約（裁定済み）。** 他の手段で `inf` を COMPLETE として書き込まれた既存 study は、
`collect()` が `TrialRecord.validate` で拒否し続ける。**orchestrator の裁定は「現状の拒否を
維持する（変更なし）」。** `run` 経由で `inf` が COMPLETE になる経路は消えたので、残るのは
外部ツールが作った study を読む場合だけで、これは我々のコードが作り得ない。再解釈機構を
足すのは AGENTS.md「起こり得ないシナリオ向けの処理を増やさない」に反する。理由文字列
（`trial 3 の value が非有限です: inf`）は診断に足りる。

### should-fix (b): 境界の int / float 混在

`_validate_numeric` が `type(bound) is not float` を拒否する。`kind="integer"` でも
境界は `2.0` と書く（裁定 4 で「整数値の float」を要求しているので一貫する）。

- `ParameterDistribution` の class docstring にこの規約とその理由を明記
- 既存テストの `low=2, high=10` を全部 `2.0 / 10.0` へ直した。裁定 4 のケース
    （`low=0.5`）は整数性判定が効く形を保つため `high` だけ float 化した
- TOML から integer 分布を書く経路のテストを追加（レビュー指摘のテスト欠落）

### should-fix (c): 引用符の閉じ忘れ

`_parse_override_value` の戻り値を `tuple[object, str | None]` に変え、TOML 解釈に
失敗した raw が `"` か `'` で始まる場合は理由を返す。裸の文字列（`monitor=mae`）と
途中に引用符を含む形（`monitor=loss"x`）は従来どおり通す。

### should-fix (d): `docs/image-based-dispense-calibration-ml-plan.md`

Hydra 前提の記述を案 C の実態へ合わせた（判断の経緯そのものは書き直していない）。

- 概要表の「設定・探索」行、`ml-train` / `ml-hpo` の依存、smoke check 項目 1 と 6
- 「設定管理」節を `ConfigComposition` + strict converter の説明へ差し替え、
    Hydra config 構成（YAML tree）を packaged config group（TOML）へ置き換え
- 「Optuna による探索」を `ml.tuning.runner` 直接駆動へ。`n_jobs` / multirun の記述を
    「複数 OS プロセスが 1 個の RDB study を共有する」へ、`optimization_results.yaml` を
    `StudyResults` document へ
- entrypoint / 運用 CLI 節、package 構成の注釈、Phase 3、受け入れ条件、参照リンク
- **Hydra を外した理由は orchestrator 記録（決定 1）の 3 点に合わせた**。初稿では
    「sweeper が固定する Optuna version が衝突する」と書いたが、記録にある事実は
    「dev release 3〜4 点の同時固定」「安定版 sweeper が `optuna<3`」
    「`BasicLauncher` が逐次 `for` ループで並列化しない」「既定値を attrs へ寄せる方針が
    resolver 等を不要にしていた」なので書き換えた

### should-fix (f) の判断: **2 箇所とも削った**

`search_space.py` の新規 `raise ValueError` 2 件は、どちらも `validate()` を通していれば
到達しない防御分岐だった。**裁定 5（到達不能な `isinstance(loaded, dict)` を削除）と
§17 に揃えて削り、narrowing だけ `cast` で済ませた。**

- `suggest` の categorical: `suggest_categorical` の戻り型は `None` を含むが、渡した
    `choices` は `tuple[Scalar, ...]` なので None は返り得ない。レビューが指摘した
    「メッセージが実態と合っていない」も同時に消える
- `_bounds`: `low` / `high` が None でないことは `validate()` が保証する

pyright は 0 errors のまま。残す判断もあり得たが、「公開経路から到達できない分岐は
置かない」という MR5 で既に下した裁定と同じ形なので、一貫性を優先した。

### 変更しない（記録のみ）

**`edge.toml` の必須フィールド（should-fix 8 / (e)）。** `max_epochs` / `monitor` は既定値を
持たないので「既定値の二重管理」には当たらない。**2 本目の profile（`trainer/gpu.toml` 等）を
足す MR で、`conf/base.toml` に必須フィールドを置いて group 層を純粋な差分にする案
（`base_names=("base",)`）を再検討する。** 現状は要求されていない機構なので足さない。

### 変異実験（新機構 14 変異・全 KILLED）

**index はこのラウンドの正本ではない**（前ラウンドの状態のまま。`git add` を使わない
指示なので今回の修正は unstaged）。そのため `git restore --worktree` は
**修正を巻き戻してしまう**ので使えない。代わりに scratchpad へ worktree の snapshot を
取り、復元は snapshot からの byte コピー + sha256 一致検証で行った。git は一切触っていない。
全 14 変異で drift ゼロ。

| # | 変異 | 結果 | 落ちたテスト |
| --- | --- | --- | --- |
| F1-a | 解釈できない URI で生の入力を素通しさせる（_split_uri の scheme 判定を外す） | **KILLED**（2 件） | `TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[operator:secret@host://db]`<br>`TestStudyStorageRedaction::test_never_raises_while_validating[operator:secret@host://db]` |
| F1-b | ``://`` が無い URI を素通しさせる（separator 判定を外す） | **KILLED**（6 件） | `TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[operator:secret@host/db]`<br>`TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[operator:secret@host://db]`<br>…他 4 件 |
| F1-c | userinfo に生の / が混じった形の検出（redacted の @ 検査）を削る | **KILLED**（5 件） | `TestStudyStorageRedaction::test_falls_back_to_a_fixed_representation_when_it_cannot_parse[postgresql://operator:sec/secret@db.example/hpo]`<br>`TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[sqlite:///operator:secret@relative.db]`<br>…他 3 件 |
| F1-d | sqlite 分岐の理由文字列を redacted_uri から生 URI へ戻す | **KILLED**（6 件） | `TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[sqlite:///operator:secret@relative.db]`<br>`TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[sqlite:///relative.db#secret]`<br>…他 4 件 |
| F1-e | in-memory 判定の理由文字列を生 URI へ戻す | **KILLED**（2 件） | `TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[sqlite://?password=secret]`<br>`TestStudyStorageRedaction::test_never_raises_while_validating[sqlite://?password=secret]` |
| F1-f | urlsplit へ戻す（不正な IPv6 表記で例外が飛ぶ） | **KILLED**（3 件） | `TestStudyStorageRedaction::test_never_carries_a_credential_into_the_redacted_uri[postgresql://operator:secret@[::1/hpo]`<br>`TestStudyStorageRedaction::test_never_raises_while_redacting[postgresql://operator:secret@[::1/hpo]`<br>…他 1 件 |
| F2-a | log スケールで step != 1 を拒否する判定を削る | **KILLED**（3 件） | `TestHyperparameterSearchValidation::test_reports_a_search_space_that_optuna_cannot_sample`<br>`TestParameterDistributionValidation::test_reports_a_logarithmic_integer_with_a_step`<br>…他 1 件 |
| F2-b | run の「完走 trial が 0 件」ガードを削る | **KILLED**（1 件） | `TestHyperparameterSearchRun::test_reports_a_search_where_no_trial_completed` |
| F2-c | state の内訳を落として診断情報を失わせる | **KILLED**（1 件） | `TestHyperparameterSearchRun::test_reports_a_search_where_no_trial_completed` |
| F3 | 既存 study との direction 照合を削る | **KILLED**（1 件） | `TestHyperparameterSearchRun::test_reports_an_existing_study_with_a_different_direction` |
| Sa | objective の非有限値チェックを削る（inf を成果物へ載せる） | **KILLED**（1 件） | `TestHyperparameterSearchRun::test_records_a_non_finite_objective_value_as_a_failure` |
| Sb | 境界の float 型照合を削る（low=2 と low=2.0 が別 fingerprint のまま通る） | **KILLED**（4 件） | `TestParameterDistributionValidation::test_reports_a_bound_written_as_an_integer`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution20]`<br>…他 2 件 |
| Sc | 引用符で始まる token の拒否を削る（fallback で黙って採用させる） | **KILLED**（3 件） | `TestApplyOverrides::test_reports_a_value_whose_quote_is_not_closed[trainer.monitor="]`<br>`TestApplyOverrides::test_reports_a_value_whose_quote_is_not_closed[trainer.monitor="loss]`<br>…他 1 件 |
| Sd | 引用符の判定を広げて裸の文字列まで拒否する（対照テストの検出力） | **KILLED**（7 件） | `TestApplyOverrides::test_accepts_a_bare_string_without_quotes`<br>`TestApplyOverrides::test_interprets_values_with_the_toml_scalar_rules[value=mae-mae]`<br>…他 5 件 |

`Sd`（引用符判定を広げて裸の文字列まで拒否する）は「過剰拒否」方向の変異で、
裁定 1 の対照テスト群が 7 件落ちる。新機構が裁定 1 を壊していないことの確認。

### 検証（差し戻し対応後）

- テスト: **832 passed / 1 skipped**（前ラウンド 763 から +69）
- `uv run pyright src/ml tests/ml scripts/ml_smoke.py` → **0 errors, 0 warnings**
- `uv run python scripts/ml_smoke.py` → すべて通過（`packaged configuration  1 group（trainer=edge）`）
- ruff 0.8.4 `check` / `format --check`、docformatter 1.7.5 `--check`（**exit 0 = 書き換えゼロ**）、
    codespell、pyupgrade、mdformat（docs のみ。`memory/agents/` は exclude 済み）→ すべて clean
- `grep -rn "殟\|憅" src/ml tests/ml` → 空

**docformatter の文字化け対策で 2 箇所の docstring を書き直した。** `optuna が...` /
`log スケールの...` のように**小文字の識別子で summary を始めると先頭が大文字化される**
（既知の落とし穴）。`` ``validate`` で optuna が... `` / `` ``log`` スケールの... `` へ直して
書き換えゼロにした。`ruff format` が 4 file を整形したので、その後に全 hook を再実行し、
変異実験も最終バイト列に対して 14 件すべて回し直した。

## code-reviewer 2 巡目（approve）後の最終ラウンド

レビュー記録の 2 巡目節: `memory/agents/code-reviewer/ml-core-5-config-tuning.md`。
verdict は approve でマージ非阻害。新規 should-fix 2 件と nit 4 件を裁定に従って直した。

### #10 「記録できない URI は受けない」を不変条件にした

password に percent-encode されていない `/` を含む URI は SQLAlchemy が正常に解釈する
実用的な形（`make_url` が password=`sec/secret` と読む）なのに、`redacted_uri` が
`<解釈できない storage URI>` へ落ちて成果物から storage の識別情報が消えていた。

**裁定どおり解析規則（RFC 3986 に忠実）は変えず、`validate()` が拒否する側で塞いだ。**
不変条件として表現できた:

```python
if self.redacted_uri == _UNREADABLE_URI:
    return (
        "storage URI を秘匿した形で記録できません。userinfo に含まれる "
        "'/' と '@' は percent-encode してください（'/' は %2F）"
    )
```

- 理由文字列で percent-encode を促す（`'/'` は `%2F`）
- `validate` の docstring に「`redacted_uri` が代替表現へ落ちる URI は必ず拒否する」を明記
- テストは含意そのものを固定した。`CREDENTIAL_URIS` + `ACCEPTED_URIS` + `REJECTED_URIS`
    の全 URI に対して
    `storage.validate() is not None or storage.redacted_uri != UNREADABLE_URI`
    （受理 ⇒ 記録できる）。受理される URI を含む corpus なので空虚にならない
- 対照として `postgresql://operator:sec%2Fsecret@db.example/hpo` は受理され、
    `redacted_uri` が `postgresql://db.example/hpo` を返すことを固定した

### #12 大文字 scheme を拒否する

`_split_uri` の `scheme.lower()` を外した。`SQLITE:////var/lib/x.db` は許可リストに
当たらず `未対応の storage scheme です: 'SQLITE'` で拒否される。optuna 側の
`NoSuchModuleError`（例外）ではなく理由文字列で返る。`_split_uri` の docstring に
小文字化しない理由を書いた。

### nit 1〜4

1. **全滅時の理由のテストを直した。** `assert "3" in error` は先行する `"FAIL=3"` で
    満たされてしまうため、要求 trial 数と state 内訳を別々に観測する形（`"FAIL=3"` と
    `"3 件を要求"`）へ変えた。変異 `Rn1` で効果を実測
2. **`_bounds` を削除して `suggest` へ畳んだ。** cast 2 個だけの helper だったので、
    categorical 分岐を先に置き、数値分岐の直前で `low, high` を 1 回だけ narrowing する
    形にした。公開インターフェースは変えていない
3. **`float(self.low).is_integer()` の `float(...)` を外した。** 直前の
    `type(bound) is not float` が型を保証するので冗長だった
4. **docs を 2 点直した。** 決定 1 の筆頭根拠（**pin していた `hydra-core` 1.3.6 +
    `hydra-optuna-sweeper` 1.4.0.dev9 が実測で壊れている。sweeper dev9 が呼ぶ
    `_execution_whitelist_` が hydra-core 1.3.6 に存在しない**）を「Hydra を外した理由」の
    第 1 項として追加し、3 点 → 4 点にした。「TPE sampler、固定 seed、`trial_count=30`」は
    実装に合わせて「sampler は Optuna の既定（TPE）に任せ、sampler / seed を渡す口は
    設けない」「`trial_count` の既定は 20」へ修正した（口を足すのではなく記述を直した）。
    前ラウンドで入れた半角空白の不統一（`同梱group を`）も直した

### 変異実験（8 変異）

| # | 変異 | 結果 | 落ちたテスト |
| --- | --- | --- | --- |
| R10-a | 「記録できない URI は受けない」不変条件のガードを削る | **KILLED**（3 件） | `TestStudyStorageRedaction::test_accepts_only_a_uri_it_can_record[postgresql://operator:sec/secret@db.example/hpo]`<br>`TestStudyStorageRedaction::test_never_carries_a_credential_into_a_reason_string[postgresql://operator:sec/secret@db.example/hpo]`<br>…他 1 件 |
| R10-b | percent-encode 済みの userinfo まで拒否する（過剰拒否の対照） | **KILLED**（2 件） | `TestStudyStorage::test_accepts_persistent_storage[postgresql://operator:sec%2Fsecret@db.example/hpo]`<br>`TestStudyStorageRedaction::test_accepts_the_same_userinfo_once_it_is_percent_encoded` |
| R12 | _split_uri で scheme を小文字化して大文字 scheme を通す | **KILLED**（4 件） | `TestStudyStorage::test_reports_an_upper_case_scheme[POSTGRESQL://host/db]`<br>`TestStudyStorage::test_reports_an_upper_case_scheme[SQLITE:////var/lib/pcbasm/hpo/study.db]`<br>…他 2 件 |
| Rn1 | 全滅時の理由から「N 件を要求」を削る（nit 1 の補強が効くか） | **KILLED**（1 件） | `TestHyperparameterSearchRun::test_reports_a_search_where_no_trial_completed` |
| Rn2-a | 畳んだ suggest から suggest_int の step を落とす（旧 #28 の再確認） | **初回 SURVIVED → テスト補強後 KILLED**（3 回連続で再現） | `TestSuggest::test_suggests_an_integer_on_the_step_grid` |
| Rn2-b | 畳んだ suggest から suggest_float の log を落とす（旧 #29 の再確認） | **KILLED**（1 件） | `TestSuggest::test_registers_the_logarithmic_scale_with_optuna[True]` |
| Rn2-c | 畳んだ suggest で categorical 分岐を最後へ戻す（分岐順の退行） | **KILLED**（1 件） | `TestSuggest::test_suggests_one_of_the_categorical_choices` |
| Rn3 | integer の整数性判定を削る（float() 除去で機構が消えていないか） | **KILLED**（2 件） | `TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution12]`<br>`TestParameterDistributionValidation::test_reports_an_inconsistent_distribution[distribution13]` |

**`Rn2-a` が本ラウンド最大の収穫。** `suggest_int` へ `step` を渡す機構を潰す変異は、
前ラウンドまで KILLED していたのに**同じテストで SURVIVED した**。原因は
`test_suggests_an_integer_on_the_step_grid` が「引いた値が `(2, 6, 10)` に入るか」しか
見ていないことで、`step` を落とすと `[2, 10]` の 9 通りから引かれ、**3 通り（33%）は偶然
格子に乗る**。`_trial()` は seed を固定していないので試行ごとに結果が変わる。

`test_registers_the_logarithmic_scale_with_optuna` と同じ手当てを入れた。実 `Trial` の
`trial.distributions[name]` が `IntDistribution(step=4)` を記録していることを観測する。
補強後は 3 回連続で KILLED を再現した。**「値の範囲だけを見るテストは確率的に嘘をつく」
という前ラウンドの #29 の教訓が、別の行にも当てはまっていた。**

### 安全手順（前ラウンドと同じ理由で snapshot 方式を継続）

orchestrator から「今回は `git add` しないので index を正本にできる」と通達を受けたが、
**index は 2 巡目レビュー前に staged された状態のままで、このラウンドの修正は unstaged。**
`git restore --worktree` を使うとこのラウンドの修正が巻き戻るので使えない。
scratchpad の snapshot + sha256 一致検証で復元した（git は一切触っていない）。
全 8 変異で drift ゼロ。

**教訓として記録する。** 「index を正本にする」手順が成立するのは、変異を当てる前に
worktree と index が一致しているときだけ。ラウンドをまたぐ場合は
「復元元が worktree の現状と一致しているか」を先に確認する必要がある。

### 検証（最終）

- テスト: **866 passed / 1 skipped**（2 巡目 832 から +34）
- `uv run pyright src/ml tests/ml scripts/ml_smoke.py` → **0 errors, 0 warnings**
- `uv run python scripts/ml_smoke.py` → すべて通過
- ruff 0.8.4 `check` / `format --check`、docformatter 1.7.5 `--check`（**exit 0**）、
    codespell、pyupgrade、mdformat（docs）→ すべて clean
- `grep -rn "殟\|憅" src/ml tests/ml scripts docs` → 空

**docformatter の大文字化を 1 箇所踏んだ。** `percent-encode すれば受理し...` が
`Percent-encode ...` へ書き換えられたので `` ``%2F`` へ percent-encode すれば... `` へ直した。
**小文字の識別子・英単語で summary を始めない**という規則は、このラウンドで 3 例目。
