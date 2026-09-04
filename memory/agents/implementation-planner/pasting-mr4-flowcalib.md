# pasting 再構成 MR4: flowcalib（流量キャリブレーション）

計画の正典: `/home/gop/.claude/plans/claude-codex-src-pcbasm-pasting-pasting-sprightly-tome.md`（ユーザー承認済み）。
この文書はその MR4 部分を実装単位に落としたもの。作業ディレクトリは worktree
`/tmp/pcb-assembly-worktrees/pasting-mr4`（ブランチ `refactor/2026-09-03/pasting-mr4-flowcalib`、
MR3 ブランチ `refactor/2026-09-03/pasting-mr3-applicator-session` から分岐）。

MR5（dataset / toolhead_offset）が別 worktree で並行している。**書込範囲を守る**（下記）。

## 目的

流量キャリブレーションの数理が `pasting/calibration.py` と `pasting/dispense_calibration.py` に
分裂し、機械手順（銅板 transform・removal Z・線描画ループ）が web ジョブ
`web/api/jobs/pasting/dispense_calibration.py`（922 行）に流出している。これを
`pasting/flowcalib/` に集約し、web ジョブを対話・ループ・進捗だけにする。
`paste_flow_calibration_board/`（PFCB）は流量キャリブ専用基板生成なので `flowcalib/board/` に移し、
44 文字級の `PasteFlowCalibration` 接頭辞を全削除する。

## 目標ツリー

```
src/pcbasm/pasting/flowcalib/
  __init__.py     docstring のみ（re-export 無し）
  params.py       CalibrationParams（ジョブ param 既定値の唯一の出典）
  flow.py         FlowCalibration / MassFlowEstimate / estimate_mass_flow / sweep_schedule / slot_area /
                  rate_sweep_amount_ul / speed_sweep_amount_ul / RateMeasurement / DispenseRateCalibration /
                  RotationsPerUlRound / rotations_per_ul_round
  lines.py        LineLayout / validate_line_layout / RateSweepPoint / plan_rate_sweep / SpeedSweepPoint / SpeedSweep / plan_speed_sweep
  procedure.py    FlowCalibrationProcedure（銅板・transform・applicator を束ねる HAL 側手順）
  board/
    __init__.py   docstring のみ
    config.py     旧 pfcb/config.py（接頭辞削除、schema v1 維持）
    catalog.py    旧 pfcb/catalog.py
    layout.py     旧 pfcb/layout.py
    generator.py  旧 pfcb/generator.py
```

削除: `pasting/calibration.py`, `pasting/dispense_calibration.py`, `pasting/paste_flow_calibration_board/`。

## 公開 API（シグネチャ）

### flowcalib/params.py

```python
@attrs.frozen
class CalibrationParams:
    board_width: float = 40.0; board_height: float = 40.0
    line_length: float = 10.0; line_count: int = 10; line_amount_ul: float = 0.5; row_pitch: float = 3.0
    removal_z_offset: float = 0.0
    rate_min: float = 0.5; rate_max: float = 5.0; rate_divisions: int = 6
    speed_min: float = 1.0; speed_max: float = 10.0; speed_divisions: int = 6
    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> Self   # ctx.params から毎ラウンド読み直す。int 系は max(1, int(v)) に正規化。欠落は既定値
    def line_layout(self, line_count: int | None = None) -> LineLayout   # margin=LAYOUT_MARGIN_MM
    @property
    def required_line_count(self) -> int    # max(line_count, rate_divisions, speed_divisions)（現 web の _layout_for_sweep 相当）
LAYOUT_MARGIN_MM = 5.0
CONVERGENCE_REL_TOL = 0.02
```

web の `DISPENSE_CALIBRATION_DEFAULT_*` 13 定数は削除し、`ParamSpec` の default は `CalibrationParams()` の属性を参照する。

### flowcalib/flow.py（旧 calibration.py + dispense_calibration.py の数理）

```python
@attrs.frozen
class FlowCalibration:            # 旧 MassFlowCalibration / FlowCalibration / FlowCalibrationSet を 1 つに統合
    rotations: float; masses_mg: tuple[float, ...]; density_mg_per_ul: float
    @property mean_mass_mg / volume_ul / rotations_per_ul / stdev_rotations_per_ul   # 1 点なら stdev 0.0
    def dispense_rate_for(self, rotation_rate: float) -> float
    def dispense_accel_for(self, rotation_accel: float) -> float
    def rescaled_dispense_accel(self, previous_accel: float, previous_rotations_per_ul: float) -> float

@attrs.frozen
class MassFlowEstimate: ...      # 現 calibration.MassFlowEstimate と同フィールド（loading router が JSON 化する。キー名を変えない）
def estimate_mass_flow(*, mass_mg, rotations, rate, accel, density_mg_per_ul) -> MassFlowEstimate   # 非正入力は該当項目 None（現挙動維持）

def sweep_schedule(minimum: float, maximum: float, divisions: int) -> tuple[float, ...]   # 旧 dispense_rate_schedule / fill_speed_schedule（本体同一）を 1 本に
def slot_area(length: float, bead_width: float) -> float
def rate_sweep_amount_ul(rate: float, line_length: float, fill_speed: float) -> float   # 旧 rate_sweep_amount
def speed_sweep_amount_ul(line_length: float, bead_width: float, ul_per_mm2: float) -> float   # web _calibrate_max_fill_speed のインライン式を回収

@attrs.frozen class RateMeasurement: rate; measured_ul; commanded_ul; @property efficiency
@attrs.frozen class DispenseRateCalibration: measurements: tuple[RateMeasurement, ...]; drop_frac=0.10; baseline_count=3
    @property baseline_efficiency -> float | None ; max_dispense_rate -> float | None     # raise しない
@attrs.frozen class RotationsPerUlRound: previous; computed; dispense_accel; rotations_used
    @property relative_change -> float ; def converged(self, rel_tol: float = CONVERGENCE_REL_TOL) -> bool
def rotations_per_ul_round(*, mass_mg, line_count, amount_ul, previous_rotations_per_ul,
                           previous_dispense_accel, density_mg_per_ul) -> RotationsPerUlRound
```

命名: μL は `_ul`、密度は `density_mg_per_ul`（`specific_gravity` 廃止。`PasteDispenser.density_mg_per_ul` プロパティを使う）。

### flowcalib/lines.py

```python
@attrs.frozen
class LineLayout:                 # __attrs_post_init__ の raise と LineLayoutOverflowError を廃止
    line_length; line_count; row_pitch; board_width; board_height; margin: float = 0.0
    @property rows_per_column / column_pitch / max_columns / capacity / fits(bool) / lines(tuple[tuple[Point2d, Point2d], ...])
    def line(self, index: int) -> tuple[Point2d, Point2d]
def validate_line_layout(layout: LineLayout) -> str | None   # 現 web _layout_overflow_message の文言（「線 N 本は折り返しても…最大 M 本…」）を core が返す

@attrs.frozen class RateSweepPoint: index: int; rate: float; amount_ul: float; start: Point2d; end: Point2d
def plan_rate_sweep(params: CalibrationParams, *, fill_speed: float) -> tuple[tuple[RateSweepPoint, ...] | None, str | None]
@attrs.frozen class SpeedSweepPoint: index; fill_speed; amount_ul; start; end
@attrs.frozen class SpeedSweep: points: tuple[SpeedSweepPoint, ...]; total_amount_ul: float
def plan_speed_sweep(params: CalibrationParams, *, ul_per_mm2: float, bead_width: float) -> tuple[SpeedSweep | None, str | None]
```

### flowcalib/procedure.py（旧 web `_CalibrationContext` + `_removal_z` + 線描画ループ）

```python
class FlowCalibrationProcedure:
    @classmethod
    def setup(cls, result: BoardCalibrationResult) -> Self   # PasteSession.from_calibration → 外形を Copper 化して高さ計測 → PasteCorrection/Compose を組む（現 _CalibrationContext と同じ手順）
    def __enter__(self) -> Self / __exit__          # applicator の enable / disable（現 web の with 相当）
    @property session -> PasteSession ; applicator -> PasteApplicator ; rotations_per_ul -> float ; dispense_accel -> float ; transform -> Transform
    def move_to_loading_z(self) -> None
    def removal_z(self, offset: float) -> float     # max(z.min, z.max - offset)（現 _removal_z）
    def move_to_removal_z(self, offset: float) -> float
    def draw_lines(self, lines: Sequence[tuple[Point2d, Point2d]], *, amount_ul: float | Sequence[float],
                   fill_speed: float | Sequence[float] | None = None, rate_cap: float | Sequence[float] | None = None,
                   checkpoint: Callable[[], None] | None = None) -> tuple[DispenseExecution, ...]   # retract → 各線 draw_line。checkpoint は各線の前に呼ぶ（web が ctx.checkpoint を渡す）
    def adopt(self, round: RotationsPerUlRound) -> None   # rotations_per_ul / dispense_accel を更新し applicator を作り直す
```

`amount_ul` / `fill_speed` / `rate_cap` はスカラーか線ごとの列。列の長さが lines と違う場合は ValueError（invariant）。
prompt / progress / abort / log 文言は持たない（pcbasm は `web.*` を import しない）。ログは
`logging.getLogger(get_class_module_path(cls))`。

### flowcalib/board/（旧 paste_flow_calibration_board/）

- 接頭辞 `PasteFlowCalibration` を全削除: `BoardConfig`, `BoardSpec`, `PurgePadSpec`, `PatternSpec`,
  `CustomPadDraft` / `CustomPadSpec` / `CustomPadShape` / `CustomPadShapeId`, `PadPattern`, `PadCatalog`,
  `BoardLayout`, `PadLayout`, `PatternLayout`, `LayerPolygon`, `BoardGenerator`, `BoardPreview`,
  `ResolvedConfig`, `PatternAddition`; 定数 `BOARD_KIND`, `BOARD_SCHEMA_VERSION`, `CUSTOM_PAD_SHAPES`
- web 側で名前衝突するときは `from pcbasm.pasting.flowcalib import board as flowcalib_board` で参照
- `PasteFlowCalibrationBoardEnvironmentError`（= `KicadError` の別名）は削除し `pcb.footprint.FootprintLibraryError` / `pcb.units.KicadError` を直接使う
- `...OverflowError` → `build_board_layout(config, resolved) -> tuple[BoardLayout | None, str | None]`（preview は既にこの形）
- 例外は `BoardConfigError(ValueError)` のみ（`normalize_*` の invariant 用）
- 重複型 `Point/Polygon/Bounds/FootprintEnvelope` は `geometry.Point2d` / `LayerPolygon(layer, points)` / `geometry.packing.Rect` / `pcb.footprint.FootprintEnvelope` に寄せる（MR1 で大半は済んでいる。残りを確認）
- `_document_bool`（未使用）削除
- **document JSON は schema v1 のまま。キー・値を変えない**（`tests/web/api/routers/test_paste_flow_calibration_board.py`、e2e が無変更で緑になることが証明）

### web `jobs/pasting/dispense_calibration.py` の薄化

残すもの: `register` / `ParamSpec`（default は `CalibrationParams()`）、`CALIBRATION_MENU_STAGE`、
`parse_run_calib_command`、`_calibration_menu_loop`、`_handle_menu_loading_or_machine`、tare / 質量 prompt、
ラウンドループと採用/再計測の状態機械、`apply_to_machine_toml`、`_DispenseCalibrationResults`、
`_dispense_calibration_result`、`_generate_calibration_board`。

削除するもの: `DISPENSE_CALIBRATION_DEFAULT_*` 13 定数、`_CalibrationContext`、`_removal_z`、`_move_to_removal_z`、
`_build_line_layout` / `_layout_overflow_message` / `_line_layout` / `_layout_for_sweep`、
`_weighed_draw` の `draw` 本体（→ `procedure.draw_lines(...)` 1 行）。

数値の掃引・量計算は全て `flowcalib.flow` / `flowcalib.lines` を呼ぶ。web に式を残さない。

### その他の追従

- `src/web/api/routers/pasting_loading.py`: `estimate_mass_flow` の import 先を `pcbasm.pasting.flowcalib.flow` に。レスポンス JSON のキーは不変
- `src/web/api/routers/paste_flow_calibration_board.py`, `src/web/api/app.py`, `src/web/api/dependencies.py`: import と型名を新名に（router の URL・pydantic モデル・レスポンス形は変えない。ミラー縮小は MR6）
- `docs/image-based-dispense-calibration*.md`: モジュールパスの言及を更新（内容の書き換えは MR6）
- `src/web/api/jobs/pasting/__init__.py` と `common.py` は **編集しない**（MR5 と共有。必要が出たら implementer ノートに書いて orchestrator に報告）

## テスト

配置（src 1 ファイル ↔ test 1 ファイル）:

```
tests/pcbasm/pasting/flowcalib/__init__.py
tests/pcbasm/pasting/flowcalib/test_params.py    from_mapping の正規化・既定値・required_line_count
tests/pcbasm/pasting/flowcalib/test_flow.py      旧 test_calibration.py + test_dispense_calibration.py の数理部分を統合（振る舞いは維持、名前だけ追従）
tests/pcbasm/pasting/flowcalib/test_lines.py     LineLayout.fits / validate_line_layout 文言 / plan_rate_sweep / plan_speed_sweep（旧 LineLayout テストを移す）
tests/pcbasm/pasting/flowcalib/test_procedure.py FakeKlipper（tests/helpers.py）で draw_lines → retract 1 回 + 線 N 本の G-code、removal_z のクランプ、adopt 後の rotations_per_ul
tests/pcbasm/pasting/flowcalib/board/{__init__,conftest,support,test_config,test_catalog,test_layout,test_generator}.py   旧 pfcb テストを移設・改名
```

削除: `tests/pcbasm/pasting/test_calibration.py`, `test_dispense_calibration.py`, `tests/pcbasm/pasting/paste_flow_calibration_board/`。

`FlowCalibrationProcedure.setup` は `BoardCalibrationResult`（実カメラ）が要るので unit では構築せず、
`procedure.py` のコンストラクタは `setup` の材料（session, applicator 構築に必要なもの）を直接受ける形にして
FakeKlipper + 実 `XYZStage` / `PasteDispenser` で `draw_lines` / `removal_z` / `adopt` を検証する
（`test_applicator.py` の G-code 解析ヘルパを参考にする。共有したい helper は test 側の重複を許容するか
`tests/helpers.py` へ移す）。

挙動ピンとして**無変更で緑**を維持する既存テスト: `tests/web/api/jobs/test_pasting.py::TestCatalog`（ジョブ名 / params 既定値）、
`TestParseRunCalibCommand`、`tests/web/api/routers/test_paste_flow_calibration_board.py`、`tests/web/api/routers/test_pasting_loading.py`、
`tests/web/ui/test_paste_flow_calibration_board.py`。e2e `tests/e2e/test_paste_flow_calibration_board_browser.py` は最後に `make test-e2e -k paste_flow` 相当で 1 回通す
（`uv run pytest -m e2e tests/e2e/test_paste_flow_calibration_board_browser.py`）。

## 制約

- 検証は `make format && make type && make test-no-hardware`。`make test` / `pytest -m hardware` は禁止（hook で機構的に禁止）
- `pytest` を直接叩くときは常に `-m "not hardware"`
- ruff は F401 無効。未使用 import は `~/.cache/pre-commit/repo*/py_env-python3/bin/ruff check --select F401 --fix --no-cache <paths>` で個別に掃除
- 新規 ABC / Protocol は作らない。frozen クラス内のコレクションは tuple
- 検証は `str | None` / `tuple[X | None, str | None]` 返却。raise は HAL/IO と invariant のみ
- pyright: `@override` 必須（reportImplicitOverride）、private 参照は warning
- `pcbasm` は `web.*` を import しない
- **書込範囲**: `src/pcbasm/pasting/flowcalib/**`, `src/pcbasm/pasting/{calibration,dispense_calibration}.py`（削除）, `src/pcbasm/pasting/paste_flow_calibration_board/`（削除）,
  `src/web/api/jobs/pasting/dispense_calibration.py`, `src/web/api/routers/{pasting_loading,paste_flow_calibration_board}.py`, `src/web/api/{app,dependencies}.py`,
  `src/web/ui/layout.py`（必要なら）, `docs/image-based-dispense-calibration*.md`, 対応する tests。MR5 の範囲（`pasting/paste_dataset.py`, `pasting/toolhead_offset.py`, `pasting/dataset/`, `vision/crop.py`, `jobs/pasting/{dataset,toolhead_offset}.py`）には触らない
- commit は `refactor(pasting): ...` で検証通過後に。push / MR 作成は orchestrator が行う（implementer は commit まで）
- 実装ノート・計画外判断は `memory/agents/plan-implementer/pasting-mr4-flowcalib.md` に書く

## 実機確認（MR 本文用、ユーザーが実施）

`dispense_calibration` ①②③ 各 1 ラウンド（採用 → machine.toml 反映、中止 → メニュー復帰、レイアウト超過文言）。PFCB 画面で生成 / export / import。
