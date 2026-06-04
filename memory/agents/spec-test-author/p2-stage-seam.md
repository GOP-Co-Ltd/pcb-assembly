# P2 — stage motion 継ぎ目（move / Limits.contains）+ height 移行

計画: `/home/gop/.claude/plans/claude-pcbasm-hal-pcb-partitioned-fox.md`（P2）
担当: tests/ のみ（src は並列 plan-implementer 担当）

## 書いた / 変更したテスト一覧

### tests/pcbasm/hal/test_stage.py（ADD のみ。既存 legacy テストは無改変）

`class TestXYZStage` に追加:

- `test_move_emits_single_g1_for_absolute_point` — 正常系。全座標指定 `move(x=50, y=100, z=25, speed=Speed.absolute(100))` → 単一 `"G1 X50.0 Y100.0 Z25.0 F6000.0"`。現在位置解決は不要なので `mock_stage`（get_config のみ patch）を使用。
- `test_move_resolves_missing_coords_against_current_position` — エッジ。部分座標。`mock_stage_at_10`（現在位置 (10,10,10)）で `move(z=25, speed=Speed.absolute(100))` → `"G1 X10.0 Y10.0 Z25.0 F6000.0"`。
- `test_move_relative_adds_to_current_position` — エッジ。`relative=True`。`move(x=5, y=3, speed=Speed.absolute(100), relative=True)`（現在位置 (10,10,10)）→ `"G1 X15.0 Y13.0 Z10.0 F6000.0"`（z 省略→不変）。
- `test_move_defaults_speed_to_max_velocity` — 正常系。speed 未指定 → max_velocity(=300) で解決、`move(x=50, y=50, z=25)` → `"G1 X50.0 Y50.0 Z25.0 F18000.0"`（300*60）。
- `test_move_raises_when_resolved_point_out_of_limits` — 異常系。`move(x=150, y=50, z=25, speed=Speed.absolute(100))` → `pytest.raises(ValueError, match="制限外")`。

`class TestXYZStage` に追加した fixture:

- `mock_stage_at_10` — `mock_stage` と同じ limits を patch しつつ、`klipper.readonly.get_status` を `[10.0, 10.0, 10.0, 0.0]` に patch し `get_position()` を (10,10,10) に固定。`mock_stage` は無改変のまま（外科的変更）。**自前 HAL ABC（ReadonlyKlipper）の `get_config`/`get_status` を public 属性 `klipper.readonly` 経由で patch。3rd-party 表面・内部 private は patch していない**（既存 test_probe.py のパターンを踏襲）。

`class TestLimits` に追加:

- `test_contains_point_feed` — `@pytest.mark.parametrize` で `(Point3d, feed, expected)` 7 ケース。既存 Waypoint 版 `test_contains` をミラーし `limits.contains(point, feed)` を直接呼ぶ。(50,100,25)+150→True / (0,0,0)+0→True / (100,200,50)+300→True / (-1,..)→False / (..201..)→False / (..51)→False / feed=301→False。

### tests/pcbasm/pasting/test_height.py（fixture 更新のみ）

- `mock_stage` fixture: `stage.to_gcode.return_value = GCode()` → `stage.move.return_value = GCode()` に変更（height.py が `stage.move(...)` を呼び、戻り値を `+ gcode.wait(...)` で連結するため実体 GCode が必要）。他は無改変。

## 仕様根拠の対応表

- move() の各テスト → 本タスクの **PINNED API**（`XYZStage.move(x,y,z,*,speed=None,relative=False) -> gcode.GCode`）。仕様文面: 「None coords resolve against current position」「relative=True adds to current」「speed=None → max_velocity」「resolved point+feed validated via Limits.contains; out-of-limits raises ValueError(含 "制限外")」「emits ONE G1 ... same format as existing legacy tests, e.g. feed=100 → F6000.0」。
- F フォーマット根拠 → 既存 legacy テスト `test_move_returns_gcode`（同一マシンで `"G1 X50.0 Y100.0 Z25.0 F6000.0"`）= このステージの GCode 発行契約。`gcode.move` は `f"X{x}"` / `f"F{velocity*60}"`（src/pcbasm/gcode.py:111-122）。
- `test_contains_point_feed` → 計画書 P2「`Limits.contains(point: Point3d, feed: float) -> bool`、True iff x,y,z in axis limits AND feed in v limit」。既存 Waypoint 7 ケースのミラー。
- test_height.py の mock 更新 → 計画書 P2 移行表 height.py:52「`stage.move(...)` に差し替え」。

## 期待される失敗（仕様 first）= 実装が PINNED API に違反

並列 plan-implementer の `move()` は **既に着地済み**（src/pcbasm/hal/stage.py:148-197）で動作するが、**発行 GCode のフォーマットが契約違反**。4 件が赤:

| テスト | 期待（契約） | 実 impl の出力 | 原因 |
|---|---|---|---|
| test_move_emits_single_g1_for_absolute_point | `G1 X50.0 Y100.0 Z25.0 F6000.0` | `G1 X50 Y100 Z25 F6000` | 座標 int 素通し＋feed int |
| test_move_resolves_missing_coords_against_current_position | `G1 X10.0 Y10.0 Z25.0 F6000.0` | `G1 X10.0 Y10.0 Z25 F6000` | z(=25) が int、feed int |
| test_move_relative_adds_to_current_position | `G1 X15.0 Y13.0 Z10.0 F6000.0` | `G1 X15.0 Y13.0 Z10.0 F6000` | feed int |
| test_move_defaults_speed_to_max_velocity | `G1 X50.0 Y50.0 Z25.0 F18000.0` | `G1 X50 Y50 Z25 F18000.0` | 座標 int 素通し（feed は float=300.0 で偶々一致） |

根本原因（src/pcbasm/hal/stage.py の `move`）:
1. **座標**: 全座標指定パス（行191 `nx, ny, nz = x, y, z`）でユーザーの int 引数が `Point3d(nx, ny, nz)` にそのまま入る。`Point3d` に float converter が無い（transform.py:24-26 は `x: float` のみ）ため `gcode.move` が `X50`（≠`X50.0`）を発行。部分/相対パスは現在位置由来の軸だけ float になり混在する。
2. **feed**: `Speed.absolute(100).resolve(...)` は格納値 `100`(int) を素通しで返す（speed.py:62 `return self._value`）→ `F{100*60}` = `F6000`(int)。default-speed は `Speed.rate(1.0).resolve(300.0)` = `1.0*300.0` = `300.0`(float) なので feed だけ `.0` 付き。

→ **legacy `to_gcode` 系は全緑**（`Trajectory`/`Move`/`Waypoint` が内部で float 化するため）。move() 経路のみの問題。

## 実装側に求める修正（plan-implementer 向け。テストは正、src を直す）

PINNED API が「same format as legacy（`X50.0`/`F6000.0`）」と明記しており、既存 legacy テストもこの float フォーマットを契約として固定済み。よって **テストは変更しない**。`move()` の出力を float 化すること。いずれか一案で可（実装判断）:

- 案A（局所・推奨）: `move` 内で `point = Point3d(float(nx), float(ny), float(nz))`、`feed = float(...)` に正規化してから検証・発行。
- 案B: `Point3d` のフィールドに `converter=float` を付与（legacy 経路は既に float なので回帰なし。ただし影響範囲は広め）。
- 案C: `gcode.move` 側で float フォーマットを保証（他呼び出し元への影響に注意）。

いずれも `Limits.contains` の検証境界（例 z=50.0 ちょうど True / 51.0 False）には影響しない。修正後、本 4 テストは緑になる想定。

## tests/helpers.py への追加

なし。3rd-party モック不使用。自前 HAL ABC（ReadonlyKlipper）の `get_config`/`get_status` を `mocker.patch.object(klipper.readonly, ...)` で public 属性経由 fake のみ。

## 検証結果

- `make format`: pass（ruff-format / ruff いずれも Passed）。
- `uv run pyright tests/pcbasm/hal/test_stage.py`: 0 errors, 0 warnings（`_klipper` private access は public `readonly` 経由 patch に書き換えて解消済み）。
- `uv run pytest -v -m "not hardware" tests/pcbasm/hal/test_stage.py tests/pcbasm/pasting/test_height.py`:
  - **32 passed**（legacy `to_gcode`/`validate`/`limits` 全件 + 新 `Limits.contains` 7 件 + `move` 制限外 ValueError 1 件 + height 3 件）。
  - **4 failed** = 上記 move() フォーマット契約違反（仕様 first の期待赤。src 修正で緑化）。
  - 2 deselected（@mark_hardware）。
