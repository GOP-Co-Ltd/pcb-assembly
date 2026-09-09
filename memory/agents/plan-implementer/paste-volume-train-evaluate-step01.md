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
- 型検査（pyright）: **pass**（0 errors, 0 warnings, 0 informations）
- `tests/ml`: **pass**（1500 passed, 1 skipped）

step 0 単体（step 1 を stash した状態）でも同じ 3 つが pass することを確認済み
（1480 passed, 1 skipped）。`make test` / `make run` / `pytest -m hardware` は実行していない。

## 変異テストで検査が働くことを確認した箇所

| 変異 | 落ちるテスト |
| --- | --- |
| augmentation の材料からラベルを外す（step 0 以前の状態） | `test_augmentation_uses_its_labelled_material`、`test_augmentation_does_not_use_the_unlabelled_material` |
| `_PLACEMENT_ROLE` を `"augmentation"` にして材料を衝突させる | `test_placement_uses_its_labelled_material`、`test_does_not_derive_the_position_from_the_rotation`（`20 >= 28` で落ちる） |
| `_leave_one_session_out_manifest` が held-out group を train へ混ぜる | `TestSessionSplit` の 3 件 + `TestRealSessions` の fold テスト（計 4 件） |
