# P3 — keystone 完了（FillSequence / to_gcode 最終形 / Trajectory 撤去）

計画: `/home/gop/.claude/plans/claude-pcbasm-hal-pcb-partitioned-fox.md`（P3）
担当: tests/ のみ（src は並列 plan-implementer 担当。本ノート作成時点で src は既に着地済み）

## 書いた / 変更 / 削除したテスト一覧

### tests/pcbasm/hal/test_stage.py（編集）
- import: `Move, Trajectory, Waypoint` を削除し `Path` を追加（`from pcbasm.geometry import Path, Point3d`）。`Speed` は維持。
- 削除した legacy テスト（API が消滅したため）:
  - `test_validate_is_valid_when_all_in_limits`（`validate` + `Trajectory`）
  - `test_validate_returns_invalid_waypoints`（同上）
  - `test_move_returns_gcode`（`to_gcode(Trajectory)`）
  - `test_move_raises_when_out_of_limits`（`to_gcode(Trajectory)`）
  - `TestLimits.test_contains`（`Waypoint in limits` の parametrize 版）
- 追加（`TestXYZStage`、`mock_stage` fixture 使用）:
  - `test_to_gcode_emits_one_g1_per_point` — 正常系。`to_gcode(Path([Point3d(50,100,25), Point3d(60,100,25)]), speed=Speed.absolute(100))` → `to_list()` == `["G1 X50.0 Y100.0 Z25.0 F6000.0", "G1 X60.0 Y100.0 Z25.0 F6000.0"]`。**← 現状 RED（後述、src 修正待ち）**
  - `test_to_gcode_raises_when_any_point_out_of_limits` — 異常系。`Path([Point3d(150,100,25)])`（x∈[0,100] 範囲外）→ `pytest.raises(ValueError, match="制限外")`。
- 維持（無改変）: 全 limits-config テスト、`test_max_velocity`、P2 の `move()` 5 件、`TestLimits.test_contains_point_feed`、`TestScalarLimits`。

### tests/pcbasm/geometry/test_routing.py（新規）+ test_trajectory.py（削除）
- `tests/pcbasm/geometry/test_trajectory.py` を削除。`Move`/`Waypoint`/`Trajectory` のテストクラス（`TestMove`/`TestWaypoint`/`TestTrajectory`）は API 消滅により破棄。
- `sort_by_nearest` 系のみ新 `tests/pcbasm/geometry/test_routing.py` へ移設。import を `from pcbasm.geometry import Point3d, sort_by_nearest`（再エクスポート経由）に変更。中身（ケース）は無改変で移植:
  - `TestSortByNearest::test_sort_by_nearest`（parametrize 4 ケース：空/単点/昇順/降順）
  - `TestSortByNearest::test_2opt_improves_crossing_path`（2-opt が交差を解消し総距離 <7.0）

### tests/pcbasm/pasting/test_applicator.py（編集）
- `mock_stage` fixture に `stage.move.return_value = gcode.GCode()` を追加（書き換え後の `_fill` が `FillSequence.to_gcode` 経由で `stage.move` を呼ぶため。`stage.to_gcode.return_value` / `stage.max_velocity` は維持）。それ以外は無改変 → 既存アサーション（send_gcode 回数・pushpull sync=False/量・enable/disable・calibrate）は GREEN 維持。

### tests/pcbasm/pasting/test_fill_sequence.py（新規） — `class TestFillSequence`
- `mock_stage` / `mock_dispenser` fixture（いずれも pcbasm 自前 HAL ABC = `XYZStage` / `PasteDispenser` の mock。test_applicator のパターンをミラー）。`stage.move`/`stage.to_gcode`/`dispenser.pushpull` の return を実 `GCode()` に設定（`to_gcode` 内の連結が成立するため）。
- `_sequence(path)` ヘルパで具体数値の `FillSequence` を構成（total=20, retraction=10, extra=2, dispense_rate=4 → dispense_time=5; lift=3; travel=Speed.absolute(30)）。`path` は実 `Path`（2点 10mm 離れ）。
- ケース:
  - `test_fill_speed_resolves_to_length_over_dispense_time` — length=10, dispense_time=5 → `fill_speed().resolve(100)==2.0`。
  - `test_fill_speed_is_none_for_zero_length_path` — 単点 path（length=0）→ `None`。
  - `test_to_gcode_pushpull_called_twice_with_dispense_then_retraction` — `pushpull` ちょうど 2 回。1 回目 `args[0]==32.0`（=retraction+extra+total=10+2+20）かつ `kwargs["sync"] is False`、2 回目 `args[0]==-10.0`（=-retraction）。
  - `test_to_gcode_descends_then_ascends_with_explicit_coords` — `stage.move` 3 回。下降上空 (0,0,8)→塗布高 (0,0,5)→上昇 (10,0,8)、いずれも `speed=Speed.absolute(30)` 明示座標。
  - `test_to_gcode_fills_along_path_with_fill_speed` — fill_speed≠None なら `stage.to_gcode` を path 全体で 1 回。speed は fill_speed（resolve→2.0）。位置/キーワード両対応で path 取得。
  - `test_to_gcode_skips_path_fill_for_zero_length_path` — 単点 path は `stage.to_gcode` を呼ばない。ただし pushpull は 2 回行う。
  - `test_to_gcode_returns_gcode` — 戻り値が `GCode`。

## 仕様根拠の対応表

- `test_to_gcode_*`（stage）→ PINNED `XYZStage.to_gcode(path: Path, *, speed: Speed)`：「emits one `"G1 X{x} Y{y} Z{z} F{feed*60}"` per point（feed=speed.resolve(max_velocity)、Speed.absolute(100)→F6000.0）」「any point out of limits → ValueError(match "制限外")」。float フォーマット（`X50.0`/`F6000.0`）は P2 から続く既存 move/legacy テストのステージ GCode 発行契約と一致。
- test_routing 移植 → 計画書 P3「`sort_by_nearest` は `routing.py` へ。`test_trajectory.py` 削除（`sort_by_nearest` 分は `test_routing.py` へ）」+ 細目「呼び出し側 import 不変＝`from pcbasm.geometry import sort_by_nearest`」。
- test_applicator の `stage.move` 追加 → 計画書 P3「`applicator._fill` を `FillSequence` 構築＋送信に書き換え（descent/ascent は `stage.move`）」。既存アサーション（send_gcode/pushpull）の GREEN 維持が done 条件。
- test_fill_sequence 全件 → PINNED `FillSequence(...)` フィールド群 + `fill_speed()` + `to_gcode(stage, dispenser)` の発行順序仕様（接近→下降→wait_for_done→pushpull(sync=False)→[prime wait + path fill]→wait_for_done→retract→ascent）。「fill_speed=Speed.absolute(path.length()/dispense_time)（length>0 かつ total/rate>0、else None）」「pushpull 1 回目 amount=retraction+extra+total・sync=False、2 回目 -retraction」「zero-length は stage.to_gcode を呼ばない」。

## 期待される失敗（仕様 first）= 実装が PINNED API に違反

検証コマンド結果: **42 passed, 1 failed, 2 deselected（hardware）**。
唯一の RED:

| テスト | 期待（契約） | 実 impl の出力 | 原因 |
|---|---|---|---|
| `test_stage.py::test_to_gcode_emits_one_g1_per_point` | `G1 X50.0 Y100.0 Z25.0 F6000.0`（×2） | `G1 X50 Y100 Z25 F6000.0`（×2） | 座標 int 素通し |

これは **P2 ノートで既出の float フォーマット契約違反と同根**。今回 `to_gcode(path, speed)` が最終形に置換されたことで再露出した。

根本原因（src/pcbasm/hal/stage.py の最終形 `to_gcode`、行 182-195）:
- `feed = float(speed.resolve(...))`（行 188）で **feed は float 化済み** → `F6000.0` は正しい。
- しかし **点座標は Path の `Point3d` をそのまま `gcode.move(x=point.x, ...)` に渡す**（行 193-194）。呼び出し側が `Path([Point3d(50,100,25), ...])` と **int リテラル**で構築すると、`Point3d` のフィールドに `converter=float` が無い（transform.py:24-26）ため int が保持され、`gcode.move` が `X50`（≠`X50.0`）を発行する。
- 比較対象として **`move()` 経路（行 176）は既に `Point3d(float(nx), float(ny), float(nz))` で正規化済み**。`to_gcode` だけ点の float 正規化が抜けている。

## 実装側に求める修正（plan-implementer 向け。テストは正、src を直す）

PINNED API が入力 `Path([Point3d(50,100,25), ...])`（int）に対し出力 `X50.0`/`F6000.0`（float）を明記しており、P2 から続く move/legacy 契約とも一致。よって **テストは変更しない**。`to_gcode` の発行点座標を float 化すること。いずれか（実装判断、`move()` との一貫性から案 A 推奨）:

- 案A（局所・`move()` と対称・推奨）: `to_gcode` のループで各点を float 正規化してから発行。例:
  ```python
  for point in path:
      p = Point3d(float(point.x), float(point.y), float(point.z))
      commands.append(gcode.move(x=p.x, y=p.y, z=p.z, velocity=feed))
  ```
  検証も同じ正規化点で行えば limits 境界に影響なし。
- 案B（横断・影響広）: `Point3d` のフィールドに `converter=float`。legacy 撤去後は float 入力が大半なので回帰は限定的だが、transform 全体に波及するため要確認。
- 案C: `gcode.move` 側で float 整形を保証（他呼び出し元への影響に注意。`move()` は既に自前で float 化しているため不要な二重化になる）。

修正後、本 1 テストは GREEN になる想定（残り 42 は既に GREEN）。

## tests/helpers.py への追加

なし。3rd-party モック不使用。`stage`/`dispenser` の mock は pcbasm 自前 HAL ABC（`XYZStage`/`PasteDispenser`）のみで、test_applicator の既存パターンをミラー。`mock_stage`（test_stage）は ReadonlyKlipper の `get_config` を public 属性 `klipper.readonly` 経由で patch する既存 fixture をそのまま使用。

## 検証結果

- `make format`: pass（ruff-format が新規/編集ファイルを1パス整形 → 再実行で全 Passed。idempotent 確認済み）。
- `uv run pytest -v -m "not hardware" tests/pcbasm/hal/test_stage.py tests/pcbasm/geometry/test_routing.py tests/pcbasm/pasting/test_applicator.py tests/pcbasm/pasting/test_fill_sequence.py`:
  - **42 passed**（stage limits/move/contains、routing 5、applicator 7、fill_sequence 7 など）。
  - **1 failed** = `test_to_gcode_emits_one_g1_per_point`（上記 float フォーマット契約違反。仕様 first の期待赤。src 修正で緑化）。
  - **2 deselected**（@mark_hardware）。
