# src/ml の module 関数 → クラスメソッド化

ブランチ `refactor/2026-09-04/ml-namespace-methods`
（`feature/2026-09-04/ml-core-3-model-evaluation` から分岐）。

## 移した一覧

計画書の 23 件をすべて計画どおりの名前で移した。後方互換 alias は作っていない。

| 旧 | 新 | 種別 |
| --- | --- | --- |
| `ml.model.inspection.measure_model_size` | `ModelSize.measure` | classmethod |
| `ml.model.inspection.count_parameters` | `ModelSize.count_parameters` | staticmethod |
| `ml.artifact.document.unstructure_document` | `DocumentKind.unstructure` | instance |
| `ml.artifact.document.structure_document` | `DocumentKind.structure` | instance |
| `ml.artifact.document.save_document` | `DocumentKind.save` | instance |
| `ml.artifact.document.load_document` | `DocumentKind.load` | instance |
| `ml.artifact.package.publish_immutable_package` | `ImmutablePackage.publish` | classmethod |
| `ml.artifact.package.verify_immutable_package` | `ImmutablePackage.verify` | classmethod |
| `ml.artifact.package.switch_active_pointer` | `ActivePointer.switch` | classmethod |
| `ml.artifact.package.load_active_pointer` | `ActivePointer.load` | classmethod |
| `ml.artifact.package.rollback_active_pointer` | `ActivePointer.rollback` | classmethod |
| `ml.data.batch.pad_image_samples` | `PaddedBatch.pad` | classmethod |
| `ml.data.image.preprocessed_shape` | `ImageShape.preprocessed` | instance |
| `ml.data.image.augmentation_parameters` | `AugmentationRange.parameters_for` | instance |
| `ml.data.image.preprocess_image_stack` | `PreprocessedSample.preprocess` | classmethod |
| `ml.data.split.build_split_manifest` | `SplitManifest.build` | classmethod |
| `ml.data.split.save_split_manifest` | `SplitManifest.save` | instance |
| `ml.data.split.load_split_manifest` | `SplitManifest.load` | classmethod |
| `ml.data.split.build_leave_one_group_out_plan` | `LeaveOneGroupOutPlan.build` | classmethod |
| `ml.evaluation.regression.gaussian_regression_metrics` | `GaussianRegressionMetrics.measure` | classmethod |
| `ml.evaluation.regression.fit_gaussian_log_variance_offset` | `GaussianPredictions.fit_log_variance_offset` | instance |
| `ml.evaluation.slices.build_diagnostic_report` | `DiagnosticReport.build` | classmethod |
| `ml.evaluation.compile_parity.compare_eager_and_compiled` | `CompileParityResult.measure` | classmethod |

## 計画外の判断ログ

### `plan_pixel_budget_batches` は module 関数のまま残した

主対象が `Sequence[BatchShape]` という集合で、単一インスタンスの所有物にならない。
戻り値も `tuple[tuple[str, ...], ...]`（sample ID の並び）であって `BatchShape` でも
`PaddedBatch` でもない。

`BatchShape.plan_batches(shapes, ...)` にすると「1 個の形が batch 群を計画する」という
不自然な読みになり、`PaddedBatch` に載せると padding と計画という別関心が混ざる。

所有者クラスを新設する案（`BatchPlan` など）は、今回のスコープ（配置の変更のみ、
要求されていない抽象化を足さない）から外れるので採らなかった。

### 私的ヘルパは module-level のまま

`_applied_scale` / `_constraint_scale`（image）、`_usable_samples` / `_UsableSamples`
（regression）、`_write_pointer` / `_active_pointer`（package）、`_fingerprint_mismatch`
（split）は複数のクラスメソッドから共有されるため module 関数のまま残した。
公開 IF ではないので namespace 圧迫には当たらない。

### 呼び出し元の追随

- `ml.artifact.package` と `ml.data.split` の document I/O が
  `ACTIVE_POINTER_DOCUMENT.save(...)` / `SPLIT_MANIFEST_DOCUMENT.load(...)` 形に変わった
- `ml.evaluation.slices` は `GaussianRegressionMetrics.measure(...)` を呼ぶようになった
- `src/ml/__init__.py` の docstring の import 例を
  `from ml.artifact.package import ImmutablePackage` に更新した
- `src/ml/artifact/package.py` の module docstring の `rollback_active_pointer` 言及を
  `:meth:`ActivePointer.rollback`` に更新した

循環 import は発生しなかった（`ml.data.split` → `ml.artifact.document` は従来どおり）。

## テストクラス名の追随

| 旧 | 新 |
| --- | --- |
| `TestCountParameters` | `TestModelSizeCountParameters` |
| `TestMeasureModelSize` | `TestModelSizeMeasure` |
| `TestUnstructureDocument` | `TestDocumentKindUnstructure` |
| `TestStructureDocument` | `TestDocumentKindStructure` |
| `TestSaveAndLoadDocument` | `TestDocumentKindSaveAndLoad` |
| `TestPublishImmutablePackage` | `TestImmutablePackagePublish` |
| `TestVerifyImmutablePackage` | `TestImmutablePackageVerify` |
| `TestPadImageSamples` | `TestPaddedBatchPad` |
| `TestPreprocessedShape` | `TestImageShapePreprocessed` |
| `TestAugmentationParameters` | `TestAugmentationRangeParametersFor` |
| `TestPreprocessImageStack` | `TestPreprocessedSamplePreprocess` |
| `TestBuildSplitManifest` | `TestSplitManifestBuild` |
| `TestValidateSplitManifest` | `TestSplitManifestValidate` |
| `TestSplitManifestFile` | `TestSplitManifestSaveAndLoad` |
| `TestLeaveOneGroupOutPlan` | `TestLeaveOneGroupOutPlanBuild` |
| `TestGaussianRegressionMetrics` | `TestGaussianRegressionMetricsMeasure` |
| `TestFitGaussianLogVarianceOffset` | `TestGaussianPredictionsFitLogVarianceOffset` |
| `TestBuildDiagnosticReport` | `TestDiagnosticReportBuild` |
| `TestCompareEagerAndCompiled` | `TestCompileParityResultMeasure` |

`TestModelSize`（`giga_multiply_accumulate` の契約）と `TestActivePointer`
（switch / load / rollback をまとめて見る）はクラス名が実体を指したままなので変えていない。

## 他 implementer への IF 変更通知

`src/ml/` の公開 IF が上表のとおり全面的に変わった。`src/ml/` を参照する実装が
他ブランチにある場合は追随が要る。現時点で `src/pcbasm/` `src/web/` からの参照は
（`ml` はドメイン非依存の基盤で依存の向きが一方向のため）存在しない。

## 既知の制約・残課題

- `memory/agents/**` の過去ノート（`ml-core-*`）には旧関数名が残る。
  README のとおり「書かれた時点の記録」として更新していない
- `plan_pixel_budget_batches` の扱いは上記の判断ログのとおり。所有者クラスを
  設ける価値があるなら別タスクで検討する

## 検証結果

- make format: pass（2 回連続で無変更）
- make type: pass（0 errors, 0 warnings）
- make test-no-hardware: pass（3127 passed, 140 deselected。件数は着手前と同じ）

## コミット

| hash | 件名 |
| --- | --- |
| `42eca3c` | `refactor(ml): move model size measurement onto ModelSize` |
| `0da3ece` | `refactor(ml): move document envelope I/O onto DocumentKind` |
| `78fe3c6` | `refactor(ml): move package publishing onto ImmutablePackage and ActivePointer` |
| `c3f512d` | `refactor(ml): move batch padding onto PaddedBatch` |
| `29b839e` | `refactor(ml): move image preprocessing onto its value objects` |
| `d82436d` | `refactor(ml): move split building onto SplitManifest and LeaveOneGroupOutPlan` |
| `6301802` | `refactor(ml): move regression metrics onto their value objects` |
| `4f50609` | `refactor(ml): move diagnostic report building onto DiagnosticReport` |
| `2dd3d9b` | `refactor(ml): move compile parity comparison onto CompileParityResult` |
