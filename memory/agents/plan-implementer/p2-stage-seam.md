# P2 — stage motion 継ぎ目 ＋ 単点呼び出しの移行

expand-migrate。`XYZStage.move` / `Limits.contains` を **追加** し、単点系呼び出しを
`to_gcode(Move...)` → `stage.move(...)` に移行した。legacy `to_gcode(Trajectory|Move|Iterable)` /
`XYZStage.validate` / `Limits.__contains__(Waypoint)` は **未変更**（applicator が継続使用、P3 で削除）。
`applicator.py` / `trajectory.py` / `tests/` は触っていない。

## 変更ファイル一覧（before → after）

### Step 1: API 追加（src/pcbasm/hal/stage.py）
- `from .speed import Speed` を追加。
- `Limits.contains(point: Point3d, feed: float) -> bool` を追加（`__contains__(Waypoint)` と併存）。
- `XYZStage.move(x=None, y=None, z=None, *, speed=None, relative=False) -> gcode.GCode` を追加。
  - `feed = (speed or Speed.rate(1.0)).resolve(self.max_velocity)`。
  - `get_position()` は `relative or x is None or y is None or z is None` のときだけ呼ぶ
    （完全指定の絶対移動はハード不要）。
  - 範囲外は `ValueError(f"制限外の移動先です: {point}, feed={feed}")`（部分文字列「制限外」を含む）。
  - 戻り値は単一 G1：`gcode.move(x=.., y=.., z=.., velocity=feed)`。

### Step 2: 単点呼び出しの移行（posctrl / pasting）
- posctrl/board.py:177  `to_gcode(Move.from_point(ref_pos, v=mv))` → `stage.move(x=ref_pos.x, y=ref_pos.y, speed=Speed.absolute(mv))`（ref_pos は Point2d）。`Move` import 削除、`Speed` 追加。
- posctrl/position.py:97  `to_gcode(Move.from_point(target, v=mv))` → `stage.move(x=target.x, y=target.y, speed=Speed.absolute(mv))`（target は Point2d）。`Move` import 削除、`Speed` 追加。
- posctrl/offset.py:86  `to_gcode(Move.from_point(move_vector, v=mv, relative=True))` → `stage.move(x=move_vector.x, y=move_vector.y, speed=Speed.absolute(mv), relative=True)`（move_vector は Point2d）。
- posctrl/offset.py:98  `to_gcode(Move.from_point(start_pos, v=mv))` → `stage.move(x=start_pos.x, y=start_pos.y, z=start_pos.z, speed=Speed.absolute(mv))`（**start_pos は Point3d**なので z を渡す＝元の Z 復帰挙動を維持）。`Move` import 削除、`Speed` 追加。
- posctrl/setup.py:137  `to_gcode(Move(x=ref_config.x, y=ref_config.y, z=calibration.z_position))` → `stage.move(x=ref_config.x, y=ref_config.y, z=calibration.z_position)`（speed 既定=max）。`Move` import 削除（Speed 不要）。
- posctrl/tour.py:21,45  `to_gcode(Move(x=machine_pt.x, y=machine_pt.y, v=30))` → `stage.move(x=machine_pt.x, y=machine_pt.y, speed=Speed.absolute(30))`（machine_pt は Point2d、2 箇所同一）。`Move` import 削除、`Speed` 追加。
- pasting/height.py:52  `to_gcode(Move.from_point(machine_pt, v=mv))` → `stage.move(x=machine_pt.x, y=machine_pt.y, speed=Speed.absolute(mv))`（machine_pt は Point2d）。`Move` import 削除、`Speed` 追加。
- pasting/probe.py:42  `to_gcode(Move(z=z + lift))` → `stage.move(z=z + self._lift_height)`（speed 既定=max、`+ gcode.wait_for_done()` 維持）。geometry import 行（`Move` のみ）削除。

### Step 3: スクリプト移行（src/scripts/）
- scripts/pasting/paste_solder.py:166  `to_gcode(Move(0, 0, 0))` → `stage.move(x=0, y=0, z=0)`。
- scripts/pasting/paste_solder.py:169  `to_gcode(Move.from_point(pos))` → `stage.move(x=pos.x, y=pos.y, z=pos.z)`（**pos は Point3d**、`+ gcode.wait_for_done()` 維持）。import から `Move` 削除。
- scripts/pasting/pasting_toolhead_offset.py 5 箇所（:163 xy / :175 z / :210 xyz / :240 z / :253 xy）→ いずれも `stage.move(...)`（speed 既定=max）。center_camera/center_toolhead は Point2d。import から `Move` 削除。
- scripts/posctrl/board_tour.py:158  `to_gcode(Move(x=origin_machine.x, y=origin_machine.y, v=30))` → `stage.move(x=origin_machine.x, y=origin_machine.y, speed=Speed.absolute(30))`（origin_machine は Point2d）。import から `Move` 削除、`from pcbasm.hal import Speed` 新規追加。

## 点型の判断ログ
- 多くの呼び出しは `Point2d`（z 省略）。`Transform.apply` は overload で型保存（Point2d→Point2d / Point3d→Point3d）なので、`board_transform.apply(Point2d)` 等の結果は Point2d と確定。
- **z を渡す箇所は 3 つだけ**：offset.py:98（start_pos=get_position()→Point3d）、paste_solder.py:169（pos=get_position()→Point3d）、および明示 z 指定（setup.py:137 / pasting_toolhead_offset の z 系 / probe.py）。
- setup.py:137 の `z=calibration.z_position` は `None` を取り得るが、旧 `Move(z=None)` と同じく `stage.move(z=None)` も「現在 Z を get_position で解決して G1 に Z<current> を出す」挙動になり等価。

## 計画外の判断ログ
- **ruff の実行方法**：計画の `uv run ruff check/format` は venv に ruff が無く失敗（このプロジェクトは ruff を pre-commit 管理。`make format` 系）。代わりに `uv run pre-commit run ruff/ruff-format --files <changed>` で検証した。結果は同等（lint Passed、format は1ファイルを正規化後 Passed）。
- ruff-format が `pasting_toolhead_offset.py` の 2 箇所の複数行 `stage.move(...)` を 1 行に畳んだ（行長に収まったため）。formatter の正規化で意図どおり。再実行で idempotent（Passed）。

## 他 implementer への IF 変更通知（並列時）
- なし。pinned シグネチャ（`XYZStage.move(self, x=None, y=None, z=None, *, speed=None, relative=False)` / `Limits.contains(point, feed)`）どおりに実装。逸脱なし。
- legacy `to_gcode` / `validate` / `__contains__(Waypoint)` は意図的に残置（P3 で削除予定）。

## 既知の制約・残課題（P3 へ）
- `src/` の `Move` 残存は stage.py（legacy `to_gcode` シグネチャ）と applicator.py / trajectory.py のみ。`rg 'Move\b' src/ | rg -v 'applicator.py|trajectory.py|geometry/__init__.py|stage.py'` は空（移行漏れなし）。
- P3 で legacy `to_gcode`・`Trajectory`/`Move`/`Waypoint`・`Limits.__contains__(Waypoint)`・`ValidationResult` を削除する際、stage.py:9 の `Move`/`Trajectory`/`Waypoint` import と legacy メソッド群を撤去する。

## 検証結果
- pyright: pass（0 errors, 0 warnings ※src 変更分。フルランで test_stage.py に既存 reportPrivateUsage warning 1 件あるが tests 側＝別 agent 管轄）。
- ruff (lint): pass。
- ruff-format: pass（1ファイル正規化後、idempotent）。
- pytest: 未実行（並列 agent が tests/ を所有、merge 時にユーザーがスイート実行）。
