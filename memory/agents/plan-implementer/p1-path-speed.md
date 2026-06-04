# P1 — 基盤の値オブジェクト（Path / Speed）実装メモ

タスク: stacked refactor の P1（加算的・keystone）。`src/` のみ担当。tests/ は並列の spec-test-author 管轄のため未編集。
計画書: `/home/gop/.claude/plans/claude-pcbasm-hal-pcb-partitioned-fox.md`

## 実装したもの（純加算・既存挙動は未変更）

1. **新規 `src/pcbasm/geometry/path.py`** — `@attrs.frozen class Path`
   - `points: tuple[Point3d, ...] = attrs.field(converter=tuple)`（list / 空タプル / generator すべて受理）
   - `length()` … 連続点差の `.norm()` 合計、点 <2 で `0.0`
   - `transformed(transform)` … `Path(transform.apply(p) for p in self.points)`
   - `__len__/__iter__/__getitem__` … `self.points` へ委譲（負インデックス可、`if not path` 可）
   - import は `.transform`・`attrs`・stdlib のみ（hal/gcode/velocity 非依存）

2. **新規 `src/pcbasm/hal/speed.py`** — `@attrs.frozen class Speed`
   - private fields `_value` / `_is_fraction`、classmethod `absolute` / `rate`、`resolve(max_velocity)`
   - `rate` は `[0,1]` 範囲外で `ValueError`（メッセージに `rate` と `[0, 1]` を含む）

3. `src/pcbasm/geometry/__init__.py` … `from .path import Path` 追加、`__all__` に `"Path"`。Move/Trajectory/Waypoint/sort_by_nearest/transforms は不変。
4. `src/pcbasm/hal/__init__.py` … `from .speed import Speed` 追加、`# speed` グループで `__all__` に `"Speed"`。

## 計画書との差分（IF 逸脱なし・要確認ではない範囲の判断）

- **`transformed` の戻り型**: 計画書 md 本文は `-> Self`、ただし本タスクの pinned CONTRACT は `-> Path`（具象）。pinned CONTRACT を優先し `-> Path` とした。`@attrs.frozen` の `Path` でサブクラス前提が無いため挙動差なし。
- **`path.py` の `from typing import Self`**: pinned スタブの import 行には `Self` があるが、`transformed -> Path` のため未使用。ruff F401 を避けるため path.py からは省いた（speed.py では classmethod が `Self` を返すので保持）。
- **`Speed` の private field と init alias**: CONTRACT は「attrs が先頭 `_` を剥がして `cls(value=..., is_fraction=...)` で構築」と想定。runtime はその通りだが **pyright がデフォルトで alias を推論せず** `reportCallIssue` を出した。pinned な公開面（field 名 `_value`/`_is_fraction`、classmethod 構築、`cls(value=..., is_fraction=...)` の呼び出し形）を一切変えずに通すため、`attrs.field(alias="value")` / `attrs.field(alias="is_fraction")` を明示。暗黙 alias を明示化しただけで公開 IF は不変。
  - 既存 src に「init を通る private attrs field」の前例なし（`HeightPlane._a/_b/_c` は `init=False`）。`alias=` 採用は本ファイル発の最小判断。

- **routing.py / `sort_by_nearest` 移設は P1 から除外**: 計画書 md の P1 scope には `geometry/routing.py`（`sort_by_nearest` 移設・再エクスポート）が含まれるが、本タスクの pinned CONTRACT は 4 項目（path.py / speed.py / 2 つの `__init__`）のみで routing を含まない。CONTRACT を権威とし、かつ「純加算（既存未変更）」を厳守するため未着手とした（移設は trajectory.py からの関数移動＝既存変更を伴い加算的でない）。**routing 移設が P1 に必要なら別途指示求む**。

## 検証結果

- `uv run pyright src/pcbasm/geometry/path.py src/pcbasm/hal/speed.py` → 0 errors
- `uv run pyright`（全体）→ 0 errors（`__init__` 変更による新規エラーなし）
- `uv run pre-commit run --files <4 files>` → ruff / ruff-format / pyupgrade / docformatter ほか全 Passed
  - 注: 本環境では ruff は venv 単体に無く pre-commit 管理（`uv run ruff` は spawn 失敗）。format/lint は pre-commit 経由で実施。
- pytest は実行せず（tests は並列 agent 管轄、merge 時にユーザーがスイート実行）。
- 参考: 契約挙動を `uv run python` でスモーク確認済（構築 3 形態 / length 0・1・2+ / 負インデックス / 不変性 / rate 境界・範囲外 ValueError）。
