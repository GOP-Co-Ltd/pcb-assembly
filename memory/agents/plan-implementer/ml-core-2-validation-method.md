# ml core MR2: 値オブジェクト検証を validate() メソッドへ移す

`memory/feedback_validation_method.md` の規約適用。branch
`feature/2026-09-04/ml-core-2-data-pipeline`（HEAD 29968e1、未コミット）。

## 変更内容

| 旧（module-level 関数） | 新（メソッド） |
| --- | --- |
| `validate_image_constraints(constraints)` | `ImageConstraints.validate() -> str \| None` |
| `validate_augmentation_range(augmentation)` | `AugmentationRange.validate() -> str \| None` |
| `validate_split_ratios(ratios)` | `SplitRatios.validate() -> str \| None` |
| `validate_split_manifest(manifest, sample_groups, *, dataset_fingerprint)` | `SplitManifest.validate(sample_groups, *, dataset_fingerprint) -> str \| None` |

- 戻り値は `str | None` のまま。例外は投げない
- エラーメッセージ文言は 1 文字も変えていない（`f"maximum_scale は minimum_scale 以上が
  必要です: ..."` は 1 本の f-string に畳んだだけ）
- 各 module の `__all__` から 4 つの関数名を削除
- `build_split_manifest()` の呼び出しを `ratios.validate()` へ
- `_fingerprint_mismatch()` は module-level helper のまま。`SplitManifest.validate()` と
  `load_split_manifest()` の双方から使う

## 計画外の判断ログ

- `SplitRatios.validate()` の `getattr(self, name)` に `value: float` の明示注釈を足した。
  `getattr` の戻りが `Any` になり、pyright の strict 設定下で `math.isfinite(value)` の
  引数型が不定になるのを避けるため。挙動は不変
- `SplitManifest.validate()` は `sample_ids_for()` の直後に置いた。検証が参照する
  accessor と隣接させるため

## 変更していないもの（意図的）

- `src/ml/serialization.py` の `structure_strictly()`、`ml/artifact/package.py` の
  `verify_immutable_package()`。値オブジェクトの自己検証ではないため
- `src/pcbasm/` の既存 `validate_*` 関数。規約ファイルどおり、この規約のためだけの
  一括改名はしない
- `src/ml/data/image.py` の module-private `_validate_image_stack()`。
  値オブジェクトを持たない引数検証なので関数のままでよい（規約の 3 つ目の但し書き）

## 既知の制約・残課題

- `tests/ml/data/test_split.py` のテストクラス名が `TestValidateSplitManifest` のまま。
  旧関数名に由来する命名で、`TestSplitManifestValidation` 等が素直だが、今回の依頼範囲外
  なので触っていない。別タスクで直すなら合わせて検討する

## 検証結果

- make format: pass（2 回連続実行、2 回目も全 hook Passed で無変更）
- make type: pass（pyright 0 errors, 0 warnings）
- make test-no-hardware: pass（2940 passed, 140 deselected）
