# P3 — pasting の切れ目（keystone 完了）

## スコープ実施内容（src/ のみ）

- **新規** `src/pcbasm/pasting/fill_sequence.py`: `FillSequence`（attrs.frozen）。
  `fill_speed() -> Speed | None`、`to_gcode(stage, dispenser) -> gcode.GCode`。
  GCode は全て**明示座標**で組む（None解決を使わない＝ビルド時にハードは未移動のため）。
  発行順: descent(2 move) → `wait_for_done` → `pushpull(sync=False)` →
  〔length>0 かつ dispense_time>0〕`wait(prime_time)` + `stage.to_gcode(path)` /
  〔それ以外〕`wait(prime_time + dispense_time)` → `wait_for_done` →
  `pushpull(-retraction)` → ascent(1 move)。
- **新規** `src/pcbasm/geometry/routing.py`: `sort_by_nearest` と `_apply_2opt` を
  trajectory.py から**逐語移設**（import は `Iterable` と `Point3d` のみ）。
- **削除** `src/pcbasm/geometry/trajectory.py`（`Move`/`Waypoint`/`Trajectory`/
  `with_velocity`/`distance`/`time` 全廃）。
- `src/pcbasm/geometry/__init__.py`: trajectory import を `from .routing import
  sort_by_nearest` に置換。`__all__` から `Move`/`Trajectory`/`Waypoint` を除去。
- `src/pcbasm/pasting/__init__.py`: `FillSequence` を export 追加。
- `src/pcbasm/hal/stage.py`:
  - `to_gcode` を最終形 `(path: Path, *, speed: Speed) -> gcode.GCode` に置換
    （全点＋解決 feed を `limits.contains` 検証、外れたら `ValueError`）。
  - legacy `validate` / `ValidationResult` / `Limits.__contains__(Waypoint)` を削除。
    `Limits.contains(point, feed)` と `ScalarLimits.__contains__` は保持。
  - imports: `from pcbasm.geometry import Path, Point3d`（`Move/Trajectory/Waypoint`
    と未使用化した `Iterable` を除去）。
- `src/pcbasm/pasting/applicator.py`: `_fill` を `FillSequence` 構築＋
  `klipper.send_gcode(seq.to_gcode(stage, dispenser))` に痩せさせた。
  `_trapezoidal_time`（prime_time 算出）と他メソッドは無改変。
  imports: geometry から `Identity, Path, Transform`、hal に `Speed` 追加、
  `from pcbasm.pasting.fill_sequence import FillSequence` 追加。

## pinned IF（計画どおり・逸脱なし）

- `XYZStage.to_gcode(self, path: Path, *, speed: Speed) -> gcode.GCode`
- `FillSequence(path, total_amount, retraction, extra_amount, dispense_rate,
  dispense_accel, retraction_rate, retraction_accel, prime_time, lift_height,
  travel_speed)` + `.fill_speed()` / `.to_gcode(stage, dispenser)`

計画書のターゲットコードを**そのまま**実装。シグネチャ逸脱なし＝spec-test-author への
IF 変更通知は不要。

## 計画外の判断ログ

- なし（計画書のコードを逐語実装）。formatter（ruff-format / docformatter）が
  fill_sequence.py の長行折返しとクラス docstring の 1 行化を自動適用。意味的に等価。

## 他 implementer への IF 変更通知（並列時）

- なし。

## 既知の制約・残課題

- tests/ は触っていない（並列の spec-test-author 担当）。`test_trajectory.py` 削除 /
  `test_routing.py` 新設 / `test_fill_sequence.py` 新設 / `test_stage.py` の to_gcode
  移植は tests 側で実施される前提。**pytest 未実行**（指示どおり）。
- 実機 prime/dispense オーバーラップのタイミング検証はユーザー（実機）。

## 検証結果（src/ のみ・指示によりpytestは未実行）

- `grep -rn "Trajectory|Waypoint|\bMove\b|with_velocity" src/`: **0件**（exit 1）。
- `grep -rn "\.distance()|\.time()|ValidationResult|\.validate(" src/`: **0件**。
- `uv run pyright`: **0 errors, 0 warnings, 0 information**。
- 変更 src ファイルへの全 pre-commit フック（ruff / ruff-format / docformatter /
  pyupgrade / codespell 他）: **全 Passed**。
- 追加: 各パッケージの runtime import smoke（geometry/pasting/hal）成功。
  `geometry.__all__` に legacy vocab 無し、`pasting.__all__` に `FillSequence` 有り。
- make test: **未実行**（並列 tests agent 所管・指示どおり）。
