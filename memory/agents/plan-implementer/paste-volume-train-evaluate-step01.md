# paste_volume train / evaluate: step 0 + step 1

commit: `347acfe`（step 0）、`fb65491`（step 1）。branch `feature/2026-09-09/paste-volume-train-evaluate`。

## 実測値

### step 0: `test_does_not_derive_the_position_from_the_rotation` の `len(pairs)`

| 条件 | 60 epoch | 200 epoch |
| --- | ---: | ---: |
| 役割ラベルあり（現状） | **32** | 32 |
| 材料を共有させた（`_placement_seed` を augmentation の材料へ差し替え） | 20 | 20 |

理論上限 8 帯 x 4 位置 = 32 に 60 epoch で到達する。閾値 `>= 28` は据え置きで両者を分ける
（下げていない。epoch も増やしていない）。テスト中のコメントに両方の実測値を書いた。

### step 1: 実 5 session の LOSO fold 構成

`data/paste-volume-datasets/`、`ImageConstraints()`、`split_seed=0`、`validation_ratio=0.15`。
entry 総数 832（blank 込み）。session ごとの entry 数は 164 が 1 本、167 が 4 本。

| held-out session | test | validation | train | train ∩ test |
| --- | --- | --- | --- | --- |
| ...20260909T120737.733 | 1 session / 167 sample | 1 / 164 | 3 / 501 | 空 |
| ...20260909T145923.452 | 1 / 167 | 1 / 167 | 3 / 498 | 空 |
| ...20260908T144137.001 | 1 / 164 | 1 / 167 | 3 / 501 | 空 |
| ...20260908T153829.606 | 1 / 167 | 1 / 167 | 3 / 498 | 空 |
| ...20260909T130756.484 | 1 / 167 | 1 / 164 | 3 / 501 | 空 |

**5 fold すべて held-out 1 / validation 1 / train 3**、どの fold でも
train ∩ test と validation ∩ test が空。計画書の検算どおり。
`resolve_session(<label>)` も fingerprint 1 件へ解決する（実データで確認）。

opt-in テストは `tests/ml/paste_volume/test_task.py::TestRealSessions::
test_forms_five_folds_of_one_held_out_one_validation_three_train`（`skip_if_no_real_sessions`）。

## 計画外の判断ログ

1. **`_derived_seed` 直呼びの「4 用途の種比較」ではなく、材料（文字列）の比較 + 用途ごとの
   突き合わせにした。** `view-dropout` と `batch-plan` は `random.Random(材料文字列)` を
   直接使っていて `_derived_seed` を通らないので、「4 用途の種」を同じ型で並べられない。
   共通するのは材料の作り方なので、`tests/ml/test_seed_roles.py` は
   (a) 4 用途の材料が互いに一致しない、(b) ラベルを外すと同じ比較関数が衝突を報告する
   （自己検査）、(c) 各材料が実装の出力と実際に一致する（用途ごとに 1 件ずつ）、
   (d) ラベルを外した材料では (c) が成り立たない（自己検査）、の 4 層で固定した。
   (c) が無いと (a) は文字列表の自己完結した検査になる。
2. **`tests/ml/test_seed_roles.py` を `tests/ml/` 直下へ置いた。** 規約が
   `ml.data.image` / `ml.data.batch` / `ml.paste_volume.batch` の 3 module をまたぐため。
   private を 2 つ import するので `# pyright: ignore[reportPrivateUsage]` を行単位で付けた
   （`reportPrivateUsage = "warning"`。blanket ignore ではない）。
3. **`PasteVolumeTrainingConfig.validate` に `validation_ratio` の範囲検査を足した**
   （計画書に明記なし）。TOML から来るユーザー入力の境界なので検証対象と判断した。
   `0 < validation_ratio < 1` を要求する。
4. **`ratios.validate()` は次元によらず常に呼ぶ。** session 次元では使わない値だが、
   既定は妥当なので害が無く、分岐を増やさない方を採った。
5. **`_built_split` の session 経路は `config.held_out_session or ""` を渡す。**
   `config.validate` が None を弾いているので到達しないが、`assert` を production へ
   入れるのを避けた（`src/ml/` に `assert` は 1 つも無い）。空文字は
   `resolve_session` が「指定が空です」で落とすので、同じ理由文を 2 箇所に書かずに済む。
6. **`SplitManifest.seed` に `fold.seed`（`_derived_seed(f"{seed}:session:{value}")`）を
   入れた。** config の `split_seed` そのままだと 5 fold の manifest が seed 欄で
   区別できない。
7. **`SplitDimension` の別名に docstring を付けられない。** `type X = ...` の直後の
   文字列は `check-docstring-first` hook が「2 つ目の module docstring」として落とす。
   コメントにした。

## 既存テストの変更（仕様変更に伴うもの）

- `PasteVolumeTrainingConfig()` の既定が session 次元になり `held_out_session` 必須に
  なったので、cell 単位 split を見る既存テストは `_cell_config()`（`split_dimension="cell"`
  を明示するヘルパー）経由へ変えた。12 箇所。
- `index.sample_groups()` → `sample_groups(dimension="cell")`（test_index.py 3 箇所、
  test_task.py 3 箇所）。
- `tests/ml/paste_volume/test_index.py` の `TestSplitGroups` を `TestCellSplitGroups` へ
  改名（session 次元の `TestSessionSplitGroups` と並ぶため）。
- `TestRealSessions` の実 index 生成を module scope の `real_index` fixture へ移した。
  実 session を使うテストが 2 件になり、8320 枚の decode を 2 度やるのを避けるため。

## 他 implementer への IF 変更通知

**step 2 以降のレーンが前提にできる確定 IF**（計画書どおり、逸脱なし）:

```python
# ml.paste_volume.index
type SplitDimension = Literal["session", "cell"]
SPLIT_DIMENSIONS: tuple[SplitDimension, ...] = ("session", "cell")

class PasteVolumeSampleIndex:
    def sample_groups(self, *, dimension: SplitDimension) -> dict[str, str]: ...
    def session_values(self) -> tuple[str, ...]: ...
    def resolve_session(self, selector: str) -> tuple[str | None, str | None]: ...

# ml.paste_volume.task
@attrs.frozen
class PasteVolumeTrainingConfig:
    split_dimension: SplitDimension = "session"
    held_out_session: str | None = None
    validation_ratio: float = 0.15
    ratios: SplitRatios = SplitRatios(0.70, 0.15, 0.15)
    split_seed: int = 0
    max_batch_pixels: int = 8_388_608
    max_batch_size: int = 32

class PasteVolumeTrainingData:
    @property
    def split_dimension(self) -> SplitDimension: ...
```

**`sample_groups` には既定値が無い。** 既存の `index.sample_groups()` 呼び出しは全部
コンパイルエラーになるので、レーン B（`train.py` など）は必ず次元を明示すること。

**`resolve_session` の前頭一致は fingerprint 文字列そのもの**（`"sha256:..."` 込み）を
見る。`sample_id` の頭 12 桁（`sha256:` を除いた部分）では一致しない。CLI から人が
打つのは `session_label`（session directory 名）を想定している。

## 既知の制約・残課題

1. **`type SplitDimension = Literal[...]`（PEP 695 の type alias）を attrs field の
   annotation に使っている。** step 4 の `compose_experiment` が `make_strict_converter()` で
   `PasteVolumeDataConfig` / `PasteVolumeTrainingConfig` を structure するとき、cattrs が
   `TypeAliasType` を解決できるか未確認。`ml.data.split.SplitName` も同じ形だが、
   これまで attrs field の型としては使われていない。**step 4 の最初に 1 件だけ
   structure を試すのが安い。** 駄目なら `Literal["session", "cell"]` を field へ直書きする
   （alias は API の読みやすさのために残す）。
2. **session が 2 本以下だと session 次元の split は作れない。** `LeaveOneGroupOutPlan` が
   held-out を除いた残りに 2 group を要求するため。合成 session を使う step 5 の
   end-to-end テストは **3 session 以上**にすること（計画書の「合成 3 session」で足りる）。
3. **`SplitManifest` に次元が記録されない。** manifest 自体は sample ID の並びしか持たない
   ので、cell manifest を session 次元で読み直すと「group が複数 split にまたがって
   います」という間接的な理由で落ちる（テスト済み）。読み違えはしないが、理由文からは
   次元の取り違えだと分かりにくい。`run_directory/config.json` に次元が残るので v1 では
   これで足りると判断した。
4. `RUNTIME_MODULES` / `DEPENDENCY_FREE_MODULES` は新 module を足していないので未変更。

## 検証結果

`make ml-docker-check`（format → 型検査 → `tests/ml`）:

- format（pre-commit 全 hook）: **pass**（再整形なし）
- 型検査（pyright）: **pass**（error / warning / information いずれも 0 件）
- `tests/ml`: **pass**（1500 passed, 1 skipped）

step 0 単体（step 1 を stash した状態）でも同じ 3 つが pass することを確認済み
（1480 passed, 1 skipped）。`make test` / `make run` / `pytest -m hardware` は実行していない。

## 変異テストで検査が働くことを確認した箇所

| 変異 | 落ちるテスト |
| --- | --- |
| augmentation の材料からラベルを外す（step 0 以前の状態） | `test_augmentation_uses_its_labelled_material`、`test_augmentation_does_not_use_the_unlabelled_material` |
| `_PLACEMENT_ROLE` を `"augmentation"` にして材料を衝突させる | `test_placement_uses_its_labelled_material`、`test_does_not_derive_the_position_from_the_rotation`（`20 >= 28` で落ちる） |
| `_leave_one_session_out_manifest` が held-out group を train へ混ぜる | `TestSessionSplit` の 3 件 + `TestRealSessions` の fold テスト（計 4 件） |

---

# レビュー 1 巡目の差し戻し対応（must-fix 6 件 / should-fix 8 件）

対象レビュー: `memory/agents/code-reviewer/paste-volume-train-evaluate-step01.md`。
裁定: `memory/agents/orchestrator/paste-volume-train-evaluate.md` の
「step 0/1 レビュー（1 巡目）の裁定」。step 2/3 の `model.py` / `PasteVolumeTask` /
`test_model.py` には触れていない。

commit: `a9736e2`（M6 + N2）、`d5b0166`（M1 + M4 + M5 + N3）、`76af20c`（M2 + M4 の
placement 観測点）、`2010ead`（M3 + S5 + S6 + S7）、`d9d21c5`（S2 + S3 + S9）。

## 実測表の訂正

**上の「step 0: `len(pairs)`」の表に書いた 60 epoch = 32 は誤り。実測は 31。**
欠けるのは `(帯 5, 位置 3)` の 1 通り。訂正後の実測値:

| 条件 | 60 epoch | 120 epoch | 200 epoch |
| --- | ---: | ---: | ---: |
| 役割ラベルあり | **31** | **32** | 32 |
| 材料を共有させた（`_PLACEMENT_ROLE="augmentation"`） | 20 | **20** | 20 |

閾値は `>= 28` をやめ、**120 epoch で `== 8 * 4` の完全一致**にした。上限に張り付くので
`>=` より強く、計画書 R1 の「閾値を下げず epoch を増やす」に沿う。

## must-fix の対応

### M1 view-dropout の材料検査

`AVAILABLE_VIEW_COUNT` を 5 → **8**。5 では `randint(1, 5)` が 5 を引き、
`sorted(sample(range(5), 5))` が乱数列に関係なく `(0,1,2,3,4)` を返していた。
8 では count 2 / `(1, 2)`、ラベルを衝突させると count 1 / `(7,)`。
7 は labelled と collided が偶然一致するので採らない。

引いた枚数そのものも観測点にした（`_expected_view_indices` が返す番号列の長さ、および
`test_view_dropout_keeps_fewer_views_than_are_available` が
`0 < len(kept) < AVAILABLE_VIEW_COUNT` を固定）。

**変異の実測**: `src/ml/data/batch.py` のラベルを `view-dropout` → `augmentation` へ
変えると、直す前は `test_seed_roles.py` が **全緑**（落ちるのは
`tests/ml/data/test_batch.py` の 1 件だけ）だったのに対し、直したあとは
`test_seed_roles.py` から **2 件**落ちる。

- `TestMaterialsInUse::test_view_dropout_uses_its_labelled_material`
  （`assert ((7,),) == ((1, 2),)`）
- `TestEveryUseIsLabelled::test_the_scan_finds_every_known_role`（走査からも消える）

### M2 閾値の根拠

`tests/ml/paste_volume/test_batch.py::TestPadding::
test_does_not_derive_the_position_from_the_rotation` を 120 epoch / `== 32` へ。
コメントに 60 epoch = 31 と衝突時 20 の両方を書いた。

**変異の実測**: `_PLACEMENT_ROLE="augmentation"` で 120 epoch でも **20**
（`assert 20 == (8 * 4)` で落ちる）。同じ変異で
`test_places_the_image_from_its_labelled_seed_material` も落ちるので、計 2 件。

### M3 session 次元での split manifest 再利用

`src/ml/paste_volume/task.py` に `_held_out_mismatch` を足し、`_resolve_split` の
読み直し経路で session 次元のときだけ「manifest の test split が
`resolve_session(held_out)` の sample 集合と一致すること」を確かめる。

**実測**: 合成 3 session で session-0 の split.json を作った後に
`held_out_session="session-2"` を要求すると、

```
既存の split manifest の test split が held_out_session と一致しません:
'session-2'（test 12 sample、要求した session 12 sample）
```

が返り `data is None`。テストは
`TestSessionSplit::test_rejects_a_manifest_saved_for_another_held_out_session`。
検査が広すぎないことの対として
`test_reuses_a_manifest_saved_for_the_same_held_out_session`（同じ fold の
読み直しは通り、同じ test split を返す）を置いた。
**変異（この検査を消す）で前者だけが 1 件落ちる**ことを実測済み。

### M4 private の直接 import

`tests/ml/test_seed_roles.py` から `_derived_seed` / `_placement_seed` の import を
消した。種を作る規則は同 module の公開関数 `derived_seed(material)` として組み直して
ある（`int.from_bytes(sha256(material).digest()[:8], "big")`）。

`placement` の観測点は `tests/ml/paste_volume/test_batch.py::TestPadding` の
`test_places_the_image_from_its_labelled_seed_material` へ移した。`collate` が返す
`valid_pixel_mask` の左上位置を、材料 → 種 → `randrange`（行 → 列）を test 内で
組み直した期待値と突き合わせる。自己検査として
`test_does_not_place_the_image_from_the_unlabelled_material` を対に置いた。

材料の組み立て（`sample_scoped_material`）と `derived_seed` は
`tests/ml/test_seed_roles.py` から import して 1 箇所に保つ。`tests/ml/paste_volume` は
ドメイン層なのでコア側の test module を import してよい（逆は
`test_architecture.py` が禁じる）。

### M5（= S1 の昇格）役割ラベルの規則の機械検証

`tests/ml/test_seed_roles.py::TestEveryUseIsLabelled` を足した。`src/ml/**/*.py` を
AST で走査し、次の 2 つの形を「種の材料を組み立てている箇所」として集める。

1. `SEED_CALLEES = ("Random", "_derived_seed", "sha256_bytes")` へ文字列
   （f-string か literal）を渡す呼び出し。`.encode()` は剥がす
2. `epoch` を差し込む f-string（呼び出し先を問わない）

各箇所の材料から、literal 部分と**差し込んだ module 直下の文字列定数**の両方を見て、
`ROLE_LABELS` のどれかが入っていることを要求する。定数を解決するのは、実装が
`_AUGMENTATION_ROLE` / `_PLACEMENT_ROLE` の形でラベルを持つため。

対象外は `EXEMPT_MATERIALS` に 1 件だけ:
`("ml.data.split", "f'{seed}:{dimension}:{value}'")`。fold seed は
`(global_seed, epoch, sample_id)` 系列ではなく、材料へ入る `dimension` そのものが
用途の区別になっている。

**走査の自己検査（4 件）**:

- `test_the_same_scan_reports_a_use_without_a_label`: tmp_path へ
  `random.Random(f"{global_seed}:{epoch}:{sample_id}")` だけの module を注入し、
  同じ走査が `labels == frozenset()` で報告することを見る
- `test_the_scan_resolves_a_label_held_in_a_module_constant`: `_ROLE = "placement"` は
  解決され、`_OTHER = "whatever"` は通らないことを 1 つの module で対にして見る
- `test_the_scan_sees_a_material_hidden_behind_encode`: `.encode()` を剥がさないと
  `placement` が丸ごと落ちる
- `test_every_exempted_material_is_still_in_the_sources`: 対象外リストが古びていないこと

加えて `test_the_scan_finds_every_known_role`（4 用途すべてを実際に見つける）と
`test_the_scan_covers_both_the_core_and_the_domain_layer` で、走査が痩せたら赤くなる
ようにしてある。

**変異の実測**: レビュアーの S20（`AugmentationRange.parameters_for` の scale だけを
`random.Random(_derived_seed(f"{global_seed}:{epoch}:{sample_id}"))` から引く 5 番目の
用途）は、直す前は `tests/ml` 全体で**全緑**だった。直したあとは `tests/ml` 1560 件の
うち **`test_every_seed_material_in_the_sources_carries_a_role_label` の 1 件だけ**が
落ち、`ml.data.image:… f'{global_seed}:{epoch}:{sample_id}'` を名指しで報告する。

### M6 docformatter が崩した日本語

指摘の 5 箇所 + `_cell_config` を直した。

**原因**: docformatter は description の段落を 1 行へ畳むとき、改行をそのまま空白へ
置き換える。日本語では語間に空白が無いので「前頭辞を 渡して」の形で残る。
`--wrap-descriptions=72` を超えない限り再整形しないので、**段落ごとに 1 物理行、
72 文字以内**へ収めれば安定する。長い段落は空行で分けた（docformatter は段落を
またいで畳まない）。

整形後に `git diff` を目視し、非 ASCII 文字が化けていないことを確認済み
（auto-memory `docformatter-corrupts-japanese`）。`pre-commit run --files` で
docformatter が **Passed**（再整形なし）になることも確認した。

## should-fix の対応

| 指摘 | 対応 | 変異で落ちるテスト |
| --- | --- | --- |
| S2 `SplitManifest(seed=fold.seed)` | `test_records_a_fold_specific_seed_in_the_manifest`（3 fold の seed が互いに違い、`split_seed=0` そのものでもない） | 1 件（`assert 1 == 3`） |
| S2 `resolve_session` の空文字ガード | `test_reports_an_empty_selector` が理由文「指定が空です」まで見る | 1 件 |
| S2 `validation_ratio` 範囲検査 | `test_reports_a_validation_ratio_outside_the_open_unit_interval`（0 / 1 / -0.1 / 1.5 / nan / inf） | 6 件 |
| S3 合成 5 session の fold 構成 | `test_forms_one_held_out_one_validation_and_three_train_from_five` が `(1, 1, 3)` を固定。`max(1, ...)` の床であることを `test_the_validation_count_grows_only_once_the_ratio_clears_one_session`（13 session で `(1, 2, 10)`）と対で示す | `max(1, ...)` を外すと 1 件 |
| S5 `groups` の二重計算 | `_leave_one_session_out_manifest` が呼び出し側の `groups` を受け取る。`index.sample_groups(dimension="session")` の作り直しを削除 | 次元をずらす変異で 8 件 |
| S6 `build` の docstring | session 経路の根拠（`LeaveOneGroupOutPlan` が held-out 1 / train・validation 各 1 以上を保証）を追記 | — |
| S7 契約外の `split_dimension` | `validate()` が `SPLIT_DIMENSIONS` を見る。`test_reports_a_split_dimension_outside_the_contract` | 1 件（外すと `TypeError`） |
| S9 理由文を見ていない異常系 | `test_reports_a_dataset_with_too_few_sessions` は「group が 2 個未満」、`test_reports_an_empty_selector` は「指定が空です」まで見る | 上記 S2 と同じ |
| N2 `_derived_seed` の docstring | 用途ごとに「材料に sample の id が入るか」「どの関数を通るか」を書き分けた。規則が掛かるのは材料の作り方であって関数ではない、と明示 | — |
| N3 `test_seed_roles.py` の記述 | view-dropout は batch 単位で、材料には batch の id 列が入る。1 sample の batch のときだけ sample 単位の 2 用途と同じ形になる、と書き直した。定数名も `SAMPLE_SCOPED_ROLES` の意味を注記 | — |

S4（`SplitManifest.seed` に 64 bit を入れた副作用）と nit（N1 / N4 / N5 / N6 / N7）は
裁定どおり対応していない。S4 は step 5 で `manifest.seed` を run seed へ流すときに
`np.random.seed` の 2^32 制限へ当たるので、**step 5 のレーンは `manifest.seed` を
そのまま run seed に使わないこと**。

## 検証結果

`make ml-docker-check`（`pre-commit run -a` → pyright → `pytest tests/ml`）:

- format: **pass**（全 hook。再整形なし）
- 型検査（pyright、`src/ml` + `tests/ml`）: **pass**（error / warning / information いずれも 0 件）
- `tests/ml`: **pass**（**1559 passed, 1 skipped**。修正前は 1544 + step 2/3 ぶん）

`make test` / `make run` / `pytest -m hardware` は実行していない。

変異は `.mutants/`（`src` と `tests/ml` の複製）へ当て、`PYTHONPATH` を差し替えて
計測した。作業ツリーは変異させていない。複製は削除済み。
`.review-mutants/`（別 agent のもの）には触れていない。
