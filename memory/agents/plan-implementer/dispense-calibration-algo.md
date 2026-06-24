# dispense_calibration（pcbasm キャリブ算出モデル）

新規 `src/pcbasm/pasting/dispense_calibration.py` の実装。算出ロジックと段ずらし
幾何のみ。HAL 非依存・全 `attrs.frozen`・`_`prefix カプセル化。スコープは本ファイル＋
新規テスト＋`__init__` re-export のみ（config/fill_sequence/applicator/settings/session は
別 agent が並行リネーム中のため一切触っていない）。

## 公開 IF（最終確定シグネチャ／C が webui で呼ぶ用）

すべて `from pcbasm.pasting import ...` で re-export 済み。

### 純粋関数

- `slot_area(length: float, bead_width: float) -> float`
  - スロット（stadium）近似 `length*bead_width + π*(bead_width/2)²` [mm²]。
  - `bead_width = nozzle_diameter * bead_width_factor` を呼び出し側で渡す。
  - applicator `_resolve_paste_height` の line モードと同一式。

- `dispense_rate_schedule(rate_min: float, rate_max: float, divisions: int) -> list[float]`
  - ②用。`rate_min..rate_max` を `divisions` 点に等間隔・昇順。
  - 入力不正（divisions<1 / rate_min<=0 / rate_max<rate_min）は **空リスト**返却（None 返却バリデーション方針）。
  - divisions==1 は `[rate_min]`。

- `fill_speed_schedule(speed_min: float, speed_max: float, divisions: int) -> list[float]`
  - ③用。`speed_min..speed_max` を `divisions` 点に等間隔・昇順。同様に不正で空リスト。

### LineLayout（① 段ずらし幾何・frozen）

- フィールド: `line_length: float (>0)`, `line_count: int (>=1)`, `row_pitch: float (>0)`,
  `origin: Point2d = Point2d(0,0)`。
- `line(index: int) -> tuple[Point2d, Point2d]` — index 番目（0始まり）の (始点, 終点)。
  範囲外は **IndexError**。各線は X 方向に line_length 伸び、Y=origin.y+index*row_pitch。
- `lines -> tuple[tuple[Point2d, Point2d], ...]`（property）— 全線まとめ。
- `span -> float`（property）— 全線の Y 総幅 `(line_count-1)*row_pitch`（1本なら 0.0）。

### DispenseRateCalibration（② 効率落ち検出・frozen）

- `RateMeasurement(rate: float (>0), measured_ul: float (>=0), commanded_ul: float (>0))`
  - `efficiency -> float`（property）= `measured_ul / commanded_ul`。
  - `measured_ul = mass / density` を呼び出し側で換算して渡す。
- `DispenseRateCalibration(measurements: tuple[RateMeasurement,...] (>=1), drop_frac: float = 0.10 (0<x<1), baseline_count: int = 3 (>=1))`
  - measurements は **レート昇順**で渡す前提。
  - `efficiencies -> tuple[float,...]`（property）。
  - `baseline_efficiency -> float`（property）= 低レート側 baseline_count 点の **median**。
  - `max_dispense_rate -> float | None`（property）— baseline*(1-drop_frac) を初めて
    下回る点の **直前のレート**。1点目で割れる/全域良なら **None**。

### FillSpeedSweep（③ 速度選択・frozen）

- `FillSpeedSweep(speeds: tuple[float,...] (>=1))`。
- `speed_at(index: int) -> float | None` — 番号→速度。範囲外は **None**。

### RotationsPerUlRound（① 収束判定・frozen）

- `RotationsPerUlRound(previous: float (>0), computed: float (>0))`。
- `relative_change -> float`（property）= `|computed-previous|/previous`。
- `converged(rel_tol: float) -> bool` — `relative_change <= rel_tol`（境界 inclusive）。

## 計画外の判断ログ

- **`RateMeasurement` を新規公開クラスとして追加**（計画では `DispenseRateCalibration` の
  「各レートの (rate, measured_ul, commanded_ul)」とのみ記載）。3つ組を素の tuple で渡すより、
  efficiency 算出と正値バリデーションを持つ frozen クラスに包む方が refactor-conventions の
  None返却/カプセル化方針に沿うため。`__init__` でも re-export 済み。
- **`max_dispense_rate` は「直前のレート」を返す**（計画通り）。「初めて drop を示す点」自体ではなく
  その 1 つ手前。1点目で割れる場合は直前が無く None。
- **schedule の不正入力は空リスト返却**（例外を投げない）。refactor-conventions の
  「try-catch より None 返却」方針の配列版。
- **`LineLayout.line` の範囲外だけ IndexError**（schedule とは別扱い）。シーケンス的アクセスの
  慣用に合わせた。collection 全体は `lines`/`span` で安全に取れる。
- **`fill_speed`/`max_fill_speed` config には非依存**（指示通り）。算出は length/rate/speed/
  efficiency などを **すべて引数で受ける**設計。config import も無し。

## 他 implementer への IF 変更通知（並列時）

- **IF 変更なし**。本ファイルは新規追加のみで、既存シンボルのシグネチャは変更していない。
- `pasting/__init__.py` への追記は **新シンボルの追加のみ**（既存 import / `__all__` 要素は
  順序含め温存。`DispenseMode` の位置など既存並びは触っていない）。他 agent が同 `__init__` を
  編集する場合、追加した import ブロック（`from .dispense_calibration import ...`）と
  `__all__` の新規 8 要素のみがコンフリクト候補。

## 既知の制約・残課題

- webui（C）側で `applicator.draw_line` を呼ぶジョブ配線は本スコープ外。本モジュールは
  純ロジックのみ。C は上記 IF を使って ①②③ のループ・prompt・ApplyPayload を組む。
- `RotationsPerUlRound` の算出（質量→rotations_per_ul）自体は既存 `FlowCalibrationSet` を
  webui 側で使う想定。本クラスは前後値の収束判定のみ担う（計画通り・FlowCalibrationSet は未改修）。

## 検証結果（スコープ内ファイルのみ・全体 make は指示により未実行）

- ruff (format/check): pass（`pre-commit run --files` で dispense_calibration.py / test / __init__.py）
- docformatter / codespell / 他フック: pass
- pyright: pass（0 errors, 0 warnings）— 3ファイル個別指定
- pytest `tests/pcbasm/pasting/test_dispense_calibration.py`: **49 passed**
  - `__init__` 経由（pytest デフォルト）でも通過。並列編集との干渉なし。
