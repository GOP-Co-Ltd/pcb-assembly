# pasting 再構成 MR5: dataset + toolhead offset

計画書: `memory/agents/implementation-planner/pasting-mr5-dataset-toolhead.md`
（正典 `/home/gop/.claude/plans/claude-codex-src-pcbasm-pasting-pasting-sprightly-tome.md`）。
作業 worktree `/tmp/pcb-assembly-worktrees/pasting-mr5`、ブランチ
`refactor/2026-09-03/pasting-mr5-dataset-toolhead`。

## 実装したもの

- `src/pcbasm/vision/crop.py`: `PolygonCrop` / `validate_crop_margins` / `crop_polygon`
  （旧 `crop_pad_image` の raise → `(None, 理由)` 返却）。`vision/__init__.py` への re-export は不要だったので無し
- `src/pcbasm/pasting/dataset/{__init__,metadata,writer,recorder,capture}.py`、`pasting/paste_volume/__init__.py`（予約 namespace）
- `src/pcbasm/pasting/paste_dataset.py` 削除
- `src/pcbasm/pasting/toolhead_offset.py`: `validate_paste_diameters` / `plan_toolhead_offset_points` の None 返却 /
  `ToolheadOffsetResult.measure -> Self | None` / `ToolheadOffsetPoint` / `ProbedPoint` / `ToolheadOffsetFailure` /
  `ToolheadOffsetDiagnostics` / `ToolheadOffsetProcedure`
- `src/web/api/jobs/pasting/dataset.py`（406 → 約 260 行）、`src/web/api/jobs/pasting/toolhead_offset.py`（418 → 約 320 行）の薄化
- `data/testing/schemas/paste_dataset_metadata_v1.json`: on-disk 形状のピン（現行 writer が書く JSON と同型）
- `__init__.py` / `common.py`（web）と MR4 範囲には触っていない

## 計画外の判断ログ

- **`ProbedPoint.point` は `Point2d` ではなく `ToolheadOffsetPoint`**。計画書は `ProbedPoint: point: Point2d; surface_z`
  だが、`deposit` が dispense 位置、`measure` が camera / board / dispense 位置を要するため、`probe()` が算出した
  座標組（board / camera / dispense）をそのまま `ProbedPoint.point` に載せた。web は `probed.point.dispense` 等で参照する
- **`ToolheadOffsetFailure.image_filename` property を追加**。診断 JSON の `failures[].image` は現行どおりファイル名
  （`toolhead_offset_failure_NN.png`）だが、`ToolheadOffsetFailure.image` は `Image | None`。命名規則を web と
  pcbasm の両方に書かないため、`index` から導く property を pcbasm 側に置き、web は PNG 保存・artifact 登録でこれを使う。
  `image is None` のときは JSON の `image` が `null`（Procedure は常に画像を撮るので実運用では起きない）
- **`PasteDatasetRecorder.record_post -> str | None`**（MR5 計画書は `-> None`、正典の master plan は `str | None`）。
  pre/post の crop 矩形不一致・pre 未記録は理由文字列で返し、web が `ValueError` に変換する。正典側に合わせた
- **`PasteDatasetRecorder.metadata` property を追加**。`finalize()` の戻りは計画どおり `Path` だが、web の
  `JobResult.summary` が計量質量・換算体積を必要とするため、確定済み metadata を読み戻せるようにした
  （web で `mass / density` を再計算しないため）
- **`PasteDatasetWriter` の公開 path 名は `working_path`（既存）を維持し `root` を追加**。計画書の「root / directory」の
  `directory` は既存名に無かったので採用せず、既存テストが使う `working_path` を残した
- **`PasteDatasetWriter.__init__(root, stem)` は副作用無し**。directory 作成・採番は `open()` に集約。ctor は
  `open()` が採番した stem を包むだけで、直接構築は想定しない（docstring に明記）
- **`DatasetCapturer` の settle は G-code `G4`（`gcode.wait`）のまま**。計画書は `time.sleep` を許容していたが、
  旧 web 実装が `gcode.wait(0.5)` だったので挙動を変えない。テストは `settle_time=0.0`
- **`allocate_volume_by_rotations` は raise のまま**。計画書のシグネチャが `-> dict[str, float]` で変更指示が無く、
  入力（正の質量・正の回転数）は呼び出し側 invariant のため
- **`validate_dataset_run(*, ...: object)`**: `is_finite_number` / `isinstance(str)` で型を確認し、`initial_purge_ul` →
  `paste_id` → 余白の順で現行文言を返す（web preflight テスト無変更で緑）
- **`DatasetRunInfo.machine_name: str | None`**（計画書は `str`）。`Machine.machine_name` と
  `PasteDatasetMachine.name` が `str | None` のため型を合わせた
- **`PasteDatasetPad.resolved` の JSON キー順が変わる**（`paste_height` と `ul_per_mm2` の順が `PasteParams` の
  フィールド順に従う）。キー集合・値は不変。`metadata.json` の読み手は dict として扱う前提なので on-disk 形状不変と判断
- **`ToolheadOffsetProcedure.roi_size` property を追加**。web の「円検出ROI: … px」ログが ROI 辺長を必要とするため
- **`measure` の unit テストは失敗経路のみ**（無地画像 → `CircleDetectionError` → `ToolheadOffsetFailure`、約 2 秒）。
  成功経路は円検出の収束が実画像に依存するため書いていない（計画書の「無理に書かない」に従う）。`probe` /
  `deposit` は `FakeKlipper` + 実 `XYZStage` / `ProbeExecutor` / `PasteApplicator` で G-code を検証
- **v1 fixture の置き場は `data/testing/schemas/`**（MR5 計画書は `data/testing/`、正典は `data/testing/schemas/`）

## 他 implementer への IF 変更通知

- `pcbasm.pasting.paste_dataset` は削除。`DatasetResolvedPaste` → `PasteParams`、`DatasetExecution` →
  `DispenseSummary`、`PadImageCrop` → `pcbasm.vision.crop.PolygonCrop`、`crop_pad_image` → `crop_polygon`
  （tuple 返却）、`validate_dataset_image_margins` → `validate_crop_margins`、`PasteDatasetMetadata.from_dict` →
  `parse_metadata`、`PasteDatasetWriter(root, …)` → `PasteDatasetWriter.open(root, …)`
- `plan_toolhead_offset_points` は `tuple[tuple[Point2d, ...] | None, str | None]`、`ToolheadOffsetResult.measure` は
  `Self | None`（いずれも raise 廃止）
- web `__init__.py` / `common.py` は無変更（MR4 との衝突無し）

## 既知の制約・残課題

- `docs/image-based-dispense-calibration-ml-plan.md:807` に旧モジュール名 `pcbasm.pasting.paste_dataset` の記述が残る
  （docs は書込範囲外。docs-keeper / code-simplifier で `pcbasm.pasting.dataset` に更新を提案）
- `ToolheadOffsetProcedure.measure` 内の `time.sleep(1.0)`（静定待ち）は旧 web 実装からの移設で、定数
  `_MEASURE_SETTLE_TIME`。テストで短縮できる引数は付けていない（成功経路の unit テストを書かないため不要と判断）
- 実機確認（ユーザー）: `paste_dataset_collection` 1 基板（metadata.json 形状・ZIP）、`toolhead_offset`
  （失敗画像 PNG / 診断 JSON / Apply）

## 検証結果

- make format: pass
- make type: pass
- make test-no-hardware: pass（2733 passed, 139 deselected）
