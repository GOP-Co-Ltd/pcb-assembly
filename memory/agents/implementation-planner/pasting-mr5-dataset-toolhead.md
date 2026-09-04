# pasting 再構成 MR5: dataset + toolhead offset

計画の正典: `/home/gop/.claude/plans/claude-codex-src-pcbasm-pasting-pasting-sprightly-tome.md`（ユーザー承認済み）。
この文書はその MR5 部分を実装単位に落としたもの。作業ディレクトリは worktree
`/tmp/pcb-assembly-worktrees/pasting-mr5`（ブランチ `refactor/2026-09-03/pasting-mr5-dataset-toolhead`、
MR3 ブランチ `refactor/2026-09-03/pasting-mr3-applicator-session` から分岐）。

MR4（flowcalib / PFCB）が別 worktree で並行している。**書込範囲を守る**（下記）。

## 目的

`pasting/paste_dataset.py`（564 行）が metadata DTO・cattrs converter・画像クロップ・writer を 1 ファイルに抱え、
web ジョブ `web/api/jobs/pasting/dataset.py`（406 行）に DTO 詰め替え（`_dataset_execution` / `_dataset_resolved`）、
50 行の Metadata 組立、キャプチャ手順が流出している。`toolhead_offset` も web ジョブ（418 行）に
probe → deposit → measure の機械手順と直径検証が残る。これらを pcbasm に回収し、web は
ループ・progress・prompt・成果物保存だけにする。

## 目標ツリー

```
src/pcbasm/pasting/dataset/
  __init__.py    docstring のみ（re-export 無し。torch 等の import 禁止）
  metadata.py    metadata.json DTO + strict cattrs converter + parse_metadata + allocate_volume_by_rotations + validate_view
  writer.py      PasteDatasetWriter（open() classmethod + context manager）
  recorder.py    PasteDatasetRecorder / DatasetRunInfo / validate_dataset_run
  capture.py     DatasetCapturer
src/pcbasm/pasting/paste_volume/__init__.py   予約 namespace（docstring のみ）
src/pcbasm/vision/crop.py                     PolygonCrop / validate_crop_margins / crop_polygon（旧 crop_pad_image）
src/pcbasm/pasting/toolhead_offset.py         既存 + validate_paste_diameters / ToolheadOffsetProcedure / ToolheadOffsetDiagnostics 等
```

削除: `pasting/paste_dataset.py`。

## 公開 API（シグネチャ）

### vision/crop.py

```python
@attrs.frozen
class PolygonCrop:
    image: ImageArray = attrs.field(eq=False); mask: ImageArray = attrs.field(eq=False); pixel_rect: tuple[int, int, int, int]
def validate_crop_margins(crop_margin_mm: float, mask_margin_mm: float) -> str | None   # 旧 validate_dataset_image_margins
def crop_polygon(image, polygon, matrix, shift, *, margin_mm: float, mask_margin_mm: float) -> tuple[PolygonCrop | None, str | None]   # 旧 crop_pad_image（raise → None 返却）
```

### dataset/metadata.py

- 旧 `paste_dataset.py` の DTO 群（`DatasetView`, `DatasetPolygon`, `DatasetCapturedView`, `PasteDatasetPad`, `PasteDatasetPurge`,
  `PasteDatasetMachine/Board/Paste/Camera/Nozzle/Config/Total`, `PasteDatasetMetadata`）を移す
- `PasteDatasetPad.resolved: PasteParams`（`DatasetResolvedPaste` 削除。JSON キーは同一 = `PasteParams.to_dict()` の 8 キー、`paste_height` は `"auto"` literal を保持）
- `PasteDatasetPad.execution` / `PasteDatasetPurge.execution: DispenseSummary`（`pcbasm.pasting.applicator.DispenseSummary`。`DatasetExecution` 削除。JSON キー同一）
- `DatasetView.__attrs_post_init__` の raise → `validate_view(view: DatasetView) -> str | None`
- `parse_metadata(data: Mapping[str, object]) -> tuple[PasteDatasetMetadata | None, str | None]`
  `schema_version` で分岐し、旧版は純関数 `_migrate_vN(doc) -> dict` で新版 dict へ写してから structure。未知版は `(None, 理由)`。
  現版は v1 のまま（**on-disk 形状不変なので bump しない**。この方針を docstring に明記）
- `allocate_volume_by_rotations(total_volume_ul: float, rotations: Mapping[str, float]) -> dict[str, float]`
- cattrs converter は strict（未知キー拒否）のまま。`PasteParams` / `DispenseSummary` の (un)structure hook を追加（`applied_mode` の `"mixed"` / None を含む）

### dataset/writer.py

```python
class PasteDatasetWriter:
    @classmethod
    def open(cls, root: Path, *, board_name: str, started_at: datetime | None = None) -> Self   # mkdir / 採番はここ。ctor の副作用を廃止
    def __enter__(self) -> Self / __exit__   # 未 finalize なら mark_incomplete()
    @property root / directory（既存の公開名を維持）
    def write_capture(self, pad_index: int, view: DatasetView, phase: Literal["pre", "post"], crop: PolygonCrop) -> DatasetCapturedView
    def finalize(self, metadata: PasteDatasetMetadata) -> Path
    def mark_incomplete(self) -> Path
```

### dataset/recorder.py（旧 job 内 dict 群 + Metadata 組立）

```python
@attrs.frozen
class DatasetRunInfo:
    machine_id: str; machine_name: str; pcb_filename: str; source_pcb: str; paste_id: str; paste_lot: str | None
    crop_margin_mm: float; mask_margin_mm: float; started_at: datetime
    dispenser: PasteDispenser; calibration: CalibrationResult
def validate_dataset_run(*, initial_purge_ul: object, paste_id: object, crop_margin_mm: object, mask_margin_mm: object) -> str | None   # web preflight の検証を回収（文言は現行維持: tests/web/api/jobs/test_pasting.py::TestPasteDatasetCollectionPreflight を無変更で緑に）

class PasteDatasetRecorder:
    def __init__(self, writer: PasteDatasetWriter, targets: DatasetTargets, views: Sequence[DatasetView]) -> None
    def record_pre(self, index: int, pad: Pad, view: DatasetView, crop: PolygonCrop) -> None
    def record_execution(self, pad_id: str, result: PasteApplicationResult) -> None
    def record_post(self, index: int, pad: Pad, view: DatasetView, crop: PolygonCrop) -> None
    def finalize(self, *, measured_mass_mg: float, run: DatasetRunInfo) -> Path   # allocate_volume_by_rotations → Metadata 組立 → writer.finalize
    def mark_incomplete(self) -> Path
```

`DatasetTargets` は `pcbasm.pasting.workflow`（MR3 で新設済み）。`targets.params_for(pad)` で `resolved` を埋める。

### dataset/capture.py（旧 job `_capture_dataset_pad`）

```python
class DatasetCapturer:
    def __init__(self, session: PasteSession, alignment_session: RegionAlignmentSession, correction: PasteCorrection, *,
                 crop_margin_mm: float, mask_margin_mm: float, settle_time: float = 0.5, frame_sink: FrameSink | None = None) -> None
    def capture(self, pad: Pad, view: DatasetView) -> tuple[PolygonCrop | None, str | None]   # session.camera_target(pad, correction, offset=view.offset) へ移動 → settle → 撮影 → crop_polygon
```

`time.sleep` は HAL 側の待ちなので capture 内で行ってよい（テストでは settle_time=0.0）。

### toolhead_offset.py（既存ファイルに追加・修正）

```python
def validate_paste_diameters(diameter_min: float, diameter_max: float) -> str | None    # 0 <= min < max（web から回収）
def plan_toolhead_offset_points(outline, *, point_count, point_spacing, edge_margin, paste_diameter_max) -> tuple[tuple[Point2d, ...] | None, str | None]   # 現 7 raise → None 返却
ToolheadOffsetResult.measure(...) -> Self | None    # raise 廃止（MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT 未満は None）

@attrs.frozen class ToolheadOffsetPoint: board: Point2d; camera: Point2d; dispense: Point2d
@attrs.frozen class ProbedPoint: point: Point2d; surface_z: float
@attrs.frozen class ToolheadOffsetFailure: index: int; board_position: Point2d; reason: str; image: Image | None

class ToolheadOffsetProcedure:      # probe → deposit → measure の 3 ステップ（ループ・progress・成果物保存は web）
    def __init__(self, result: BoardCalibrationResult, *, tolerance: float, lift_height: float,
                 diameter_min: float, diameter_max: float, point_spacing: float, frame_sink: FrameSink | None = None) -> None
    def probe(self, point: Point2d) -> ProbedPoint
    def applicator(self) -> PasteApplicator       # build_applicator(klipper, stage, dispenser_config, lift_height=)
    def deposit(self, applicator: PasteApplicator, probed: ProbedPoint, *, amount_ul: float) -> None   # 現 job の deposit_at(..., params=default_params.patched(PasteParamsPatch(paste_height=surface_z + paste_height)))
    def measure(self, index: int, probed: ProbedPoint) -> ToolheadOffsetSample | ToolheadOffsetFailure   # 検出リトライ（現 _TOOLHEAD_OFFSET_DETECTION_MAX_ATTEMPTS / RETRY_DELAY / MIN_FRAME_DETECTIONS）を含む

@attrs.frozen
class ToolheadOffsetDiagnostics:
    requested_point_count: int; minimum_valid_point_count: int; failures: tuple[ToolheadOffsetFailure, ...]; successful_point_count: int
    def to_dict(self) -> dict[str, Any] ; def save(self, path: Path) -> Path   # 現 job が書く診断 JSON と同じ形
```

### web の薄化

- `jobs/pasting/dataset.py`: `_dataset_execution` / `_dataset_resolved` / `_capture_dataset_pad` / Metadata 組立を削除し、
  `validate_dataset_run` → `plan_dataset_targets`（既存）→ `prepare_paste_workflow`（common）→ `DatasetCapturer` + `PasteDatasetRecorder` を回すだけにする。
  prompt（確認・質量入力）、progress、ZIP 化、`JobResult.summary` は web に残す。metadata.json の出力形状は不変
- `jobs/pasting/toolhead_offset.py`: 直径検証・probe/deposit/measure 手順・診断 dict 組立を削除し、`ToolheadOffsetProcedure` のループ + 失敗画像 PNG 保存 + `ToolheadOffsetDiagnostics.save` + `ApplyPayload` に絞る
- `src/web/api/jobs/pasting/__init__.py` と `common.py` は **編集しない**（MR4 と共有。必要が出たら implementer ノートに書いて orchestrator に報告）

## テスト

配置（src 1 ファイル ↔ test 1 ファイル）:

```
tests/pcbasm/vision/test_crop.py                     旧 test_paste_dataset.py の TestCropPadImage を移す（None 返却化に追従）
tests/pcbasm/pasting/dataset/__init__.py
tests/pcbasm/pasting/dataset/test_metadata.py        DTO roundtrip、parse_metadata（v1 の実ファイルを data/testing/ に置く。未知版 → (None, 理由)、未知キー拒否）、allocate_volume_by_rotations、validate_view
tests/pcbasm/pasting/dataset/test_writer.py          open() の採番・mkdir、write_capture、finalize、context manager の mark_incomplete
tests/pcbasm/pasting/dataset/test_recorder.py        record_* → finalize で metadata.json の形状（キー集合）が現行と同一。validate_dataset_run の文言
tests/pcbasm/pasting/dataset/test_capture.py         FakeCamera（tests/helpers.py）+ FakeKlipper + 実 XYZStage で capture が camera_target へ移動し crop を返す
tests/pcbasm/pasting/test_toolhead_offset.py         既存 + validate_paste_diameters / plan_toolhead_offset_points の None 返却 / measure の None / Diagnostics.to_dict
```

削除: `tests/pcbasm/pasting/test_paste_dataset.py`。

`ToolheadOffsetProcedure` は `BoardCalibrationResult`（実カメラ）が要るため `probe` / `measure` の unit テストは無理に書かない。
`deposit` の G-code（surface_z + paste_height の Z）は FakeKlipper + 実 `XYZStage` / `PasteDispenser` で検証できる構成にする
（ctor が result から取り出すものを個別に受ける薄い内部 ctor にしてもよい。判断は implementer ノートに記録）。

挙動ピンとして**無変更で緑**を維持する既存テスト: `tests/web/api/jobs/test_pasting.py::TestCatalog`、
`TestPasteDatasetCollectionPreflight`（preflight の文言・prompt 前失敗）、`TestMachineJobsWithoutKlipper`。

## 制約

- 検証は `make format && make type && make test-no-hardware`。`make test` / `pytest -m hardware` は禁止（hook で機構的に禁止）
- `pytest` を直接叩くときは常に `-m "not hardware"`
- ruff は F401 無効。未使用 import は `~/.cache/pre-commit/repo*/py_env-python3/bin/ruff check --select F401 --fix --no-cache <paths>` で個別に掃除
- 新規 ABC / Protocol は作らない。frozen クラス内のコレクションは tuple
- 検証は `str | None` / `tuple[X | None, str | None]` 返却。raise は HAL/IO と invariant のみ
- pyright: `@override` 必須（reportImplicitOverride）、private 参照は warning
- `pcbasm` は `web.*` を import しない。`import pcbasm.pasting` が cv2 / pcbnew / torch を引き込まない契約（`tests/test_package.py::TestPastingImportLight`）を壊さない（`vision/crop.py` は cv2 を使うので `pasting/dataset/capture.py` からの import は関数内か、`pasting/__init__` から辿れないことを確認）
- **書込範囲**: `src/pcbasm/pasting/dataset/**`, `src/pcbasm/pasting/paste_volume/`, `src/pcbasm/pasting/paste_dataset.py`（削除）, `src/pcbasm/pasting/toolhead_offset.py`, `src/pcbasm/vision/crop.py`（+ `vision/__init__.py` に re-export が要るなら最小）,
  `src/web/api/jobs/pasting/{dataset,toolhead_offset}.py`, 対応する tests, `data/testing/` のメタデータ実ファイル。MR4 の範囲（`pasting/calibration.py`, `pasting/dispense_calibration.py`, `pasting/paste_flow_calibration_board/`, `pasting/flowcalib/`, `jobs/pasting/dispense_calibration.py`, PFCB router）には触らない
- commit は `refactor(pasting): ...` で検証通過後に。push / MR 作成は orchestrator が行う（implementer は commit まで）
- 実装ノート・計画外判断は `memory/agents/plan-implementer/pasting-mr5-dataset-toolhead.md` に書く

## 実機確認（MR 本文用、ユーザーが実施）

`paste_dataset_collection` 1 基板（metadata.json 形状不変、ZIP）、`toolhead_offset`（失敗画像 / 診断 JSON、Apply）。
