# Phase 2: control 解体 → posctrl / pasting 分離

## 概要

`src/pcb_assembly/control/` を責務別に解体し、両者共通の位置合わせを `posctrl/` へ、
probe を使う pasting 専用ロジックを `pasting/` へ分離する。あわせて空の `pnp/` を新設する。
パッケージ名 `pcb_assembly` のリネームは Phase 3 で行うため、本フェーズでは触らない。
ロジックは一切変更せず、ファイル移動（`git mv`）と import パス追従のみ行う。

## 内部 import 依存の分析結果（分離可否の判定）

control 配下の各ファイルの「control 内部への」import を全て洗い出した結果、
**分離を妨げる内部依存は存在しない**。posctrl 行きと pasting 行きの境界をまたぐ依存はゼロ。

| ファイル | control 内部 import | 行き先 | 依存先の行き先 | 判定 |
|---|---|---|---|---|
| `setup.py` | `control.adjust` の `BoardTransformMeasurer, OffsetTransformMeasurer, XYPositionAdjustor` (L16-20) | posctrl | 全て posctrl | 問題なし |
| `tour.py` | `control.setup` の `BoardCalibrationResult` (L6) | posctrl | posctrl | 問題なし |
| `adjust/board.py` | なし | posctrl | — | 問題なし |
| `adjust/offset.py` | なし | posctrl | — | 問題なし |
| `adjust/position.py` | なし | posctrl | — | 問題なし |
| `adjust/height.py` | `control.probe` の `ProbeExecutor` (L7) | pasting | pasting (probe.py) | 問題なし（両方 pasting 行き） |
| `probe.py` | なし | pasting | — | 問題なし |
| `pasting/applicator.py` | `control.pasting.fill_path` の `build_paste_fill_path` (L11) | pasting | pasting | 問題なし（同パッケージ内） |
| `pasting/loading.py` | `control.pasting.applicator` の `PasteApplicator` (L5) | pasting | pasting | 問題なし（同パッケージ内） |
| `pasting/calibration.py` | なし | pasting | — | 問題なし |
| `pasting/fill_path.py` | なし | pasting | — | 問題なし |
| `pasting/toolhead_offset.py` | なし | pasting | — | 問題なし |

### 重点確認事項の結論

- **setup → height 依存**: `setup.py` は `control.adjust` から `BoardTransformMeasurer / OffsetTransformMeasurer /
  XYPositionAdjustor` の 3 つだけを import しており、**`HeightPlaneMeasurer` は import していない**。
  `adjust/__init__.py` は 4 シンボルを束ねているが、setup が実際に使うのは posctrl 行きの 3 つのみ。
  height は pasting へ安全に分離できる。
- **height → probe 依存**: `adjust/height.py` は `control.probe.ProbeExecutor` を import している。
  両者とも pasting 行きなので分離後は `pcb_assembly.pasting.probe` への相互参照になり破綻しない。

## __init__.py の現状公開シンボルと新設計

### 現状

- `control/__init__.py`: **空**（0 byte）。何も export していない。
- `control/adjust/__init__.py`: `BoardTransformMeasurer, HeightPlaneMeasurer, OffsetTransformMeasurer, XYPositionAdjustor`
- `control/pasting/__init__.py`: `FlowCalibration, PasteApplicator, ToolheadOffsetResult, interactive_loading`
  （docstring `"""ペースト塗布の制御."""` あり）

### 新 `posctrl/__init__.py` 設計

公開すべきは「他モジュール（scripts / 将来の pnp）から参照される位置合わせ API」。
現状の参照実態（後述の対応表）を踏まえ、`adjust/__init__.py` から height を除いた 3 つに加え、
従来 `control.setup` / `control.tour` から直接 import されていたシンボルも posctrl パッケージの公開面に上げる。

```python
"""Board/オフセットの位置合わせ共通制御."""

from .board import BoardTransformMeasurer
from .offset import OffsetTransformMeasurer
from .position import XYPositionAdjustor
from .setup import (
    BoardCalibrationResult,
    OffsetObserver,
    machine_session,
    setup_board_calibration,
)
from .tour import (
    display_at_point,
    interactive_display_at_point,
    wait_for_keypress,
)

__all__ = [
    "BoardCalibrationResult",
    "BoardTransformMeasurer",
    "OffsetObserver",
    "OffsetTransformMeasurer",
    "XYPositionAdjustor",
    "display_at_point",
    "interactive_display_at_point",
    "machine_session",
    "setup_board_calibration",
    "wait_for_keypress",
]
```

注: setup.py 内の `_BoardPointProber` 等の private（`_` prefix）は export しない。
旧来 scripts は `pcb_assembly.control.setup` / `pcb_assembly.control.tour` のように
**サブモジュール直 import** を多用していた。これらはサブモジュールが移動すれば
`pcb_assembly.posctrl.setup` 等で引き続き解決できるため、`__init__.py` への集約は必須ではない。
ただし規約（公開 IF を `__init__` に明示）と将来の pnp からの再利用を見据え、
位置合わせ公開 API を `posctrl/__init__.py` に集約する設計とする。
**plan-implementer はサブモジュール直 import 形式を維持してもよい**（後述の対応表は直 import 追従を基本とし、
集約 API は posctrl 内向けの整理として併設）。
→ 判断を 1 つに固定: **対応表どおりサブモジュール直 import に追従する**。`__init__.py` 集約は posctrl/pasting の
   公開面整備として併せて行うが、scripts/tests の import 文はサブモジュール直 import のまま新パスへ置換する。

### 新 `pasting/__init__.py` 設計

現状の `control/pasting/__init__.py` をそのまま踏襲（4 シンボル）。
probe / height は scripts がサブモジュール直 import で参照しているため `__init__` への追加は不要だが、
規約に沿い pasting パッケージの公開 API として `ProbeExecutor` と `HeightPlaneMeasurer` を加える。

```python
"""ペースト塗布の制御."""

from .applicator import PasteApplicator
from .calibration import FlowCalibration
from .height import HeightPlaneMeasurer
from .loading import interactive_loading
from .probe import ProbeExecutor
from .toolhead_offset import ToolheadOffsetResult

__all__ = [
    "FlowCalibration",
    "HeightPlaneMeasurer",
    "PasteApplicator",
    "ProbeExecutor",
    "ToolheadOffsetResult",
    "interactive_loading",
]
```

### 新 `pnp/__init__.py`

空ファイル（`pnp` は将来用の名前空間）。

## ファイル移動の git mv コマンド列

### src（plan-implementer 担当）

posctrl パッケージは新規ディレクトリのため、先に作成してから mv する。

```bash
cd /home/gop/pcb-assembly

# --- posctrl 新設 ---
mkdir -p src/pcb_assembly/posctrl
git mv src/pcb_assembly/control/setup.py        src/pcb_assembly/posctrl/setup.py
git mv src/pcb_assembly/control/tour.py         src/pcb_assembly/posctrl/tour.py
git mv src/pcb_assembly/control/adjust/board.py    src/pcb_assembly/posctrl/board.py
git mv src/pcb_assembly/control/adjust/position.py src/pcb_assembly/posctrl/position.py
git mv src/pcb_assembly/control/adjust/offset.py   src/pcb_assembly/posctrl/offset.py

# --- pasting 新設 ---
mkdir -p src/pcb_assembly/pasting
git mv src/pcb_assembly/control/pasting/applicator.py      src/pcb_assembly/pasting/applicator.py
git mv src/pcb_assembly/control/pasting/calibration.py     src/pcb_assembly/pasting/calibration.py
git mv src/pcb_assembly/control/pasting/fill_path.py       src/pcb_assembly/pasting/fill_path.py
git mv src/pcb_assembly/control/pasting/loading.py         src/pcb_assembly/pasting/loading.py
git mv src/pcb_assembly/control/pasting/toolhead_offset.py src/pcb_assembly/pasting/toolhead_offset.py
git mv src/pcb_assembly/control/pasting/__init__.py        src/pcb_assembly/pasting/__init__.py
git mv src/pcb_assembly/control/probe.py        src/pcb_assembly/pasting/probe.py
git mv src/pcb_assembly/control/adjust/height.py src/pcb_assembly/pasting/height.py

# --- control 残骸の削除（中身は移動済み、残るのは __init__ のみ） ---
git rm src/pcb_assembly/control/adjust/__init__.py
git rm src/pcb_assembly/control/__init__.py
# control/pasting/__init__.py は pasting へ mv 済み。空ディレクトリは git では自動消滅。
rmdir src/pcb_assembly/control/adjust src/pcb_assembly/control/pasting src/pcb_assembly/control 2>/dev/null || true

# --- 新 __init__.py 作成 ---
# posctrl/__init__.py を上記設計で新規作成（git mv で持ち込まず Write）
# pnp/__init__.py を空で新規作成
mkdir -p src/pcb_assembly/pnp
```

注意:
- `pasting/__init__.py` は `git mv` で持ち込んだ後、上記新設計（height/probe 追加）に **Edit で更新**する。
- `posctrl/__init__.py` は元の `adjust/__init__.py` をベースにせず新規 Write（中身が大きく異なるため）。
- `git mv` した `__init__.py` は履歴追跡のため mv を優先。`adjust/__init__.py` は posctrl に
  そのまま使えない（height を含み、setup/tour を含まない）ため `git rm` し、posctrl 側は新規作成。

### tests（spec-test-author 担当）

tests はミラー構造。各パッケージに `__init__.py` を持つ（既存 control/adjust/pasting にも存在）。
新設する `posctrl` / `pasting` 配下にも `__init__.py` が必要。

```bash
cd /home/gop/pcb-assembly

# --- tests/posctrl 新設 ---
mkdir -p tests/pcb_assembly/posctrl
git mv tests/pcb_assembly/control/test_setup.py tests/pcb_assembly/posctrl/test_setup.py
# board/position/offset 関連テストは現状 control/adjust 配下に専用ファイルが無い
#   （test_height.py のみ）。存在するのは下記のみ。

# --- tests/pasting 新設 ---
mkdir -p tests/pcb_assembly/pasting
git mv tests/pcb_assembly/control/adjust/test_height.py       tests/pcb_assembly/pasting/test_height.py
git mv tests/pcb_assembly/control/pasting/test_applicator.py     tests/pcb_assembly/pasting/test_applicator.py
git mv tests/pcb_assembly/control/pasting/test_calibration.py    tests/pcb_assembly/pasting/test_calibration.py
git mv tests/pcb_assembly/control/pasting/test_fill_path.py      tests/pcb_assembly/pasting/test_fill_path.py
git mv tests/pcb_assembly/control/pasting/test_loading.py        tests/pcb_assembly/pasting/test_loading.py
git mv tests/pcb_assembly/control/pasting/test_toolhead_offset.py tests/pcb_assembly/pasting/test_toolhead_offset.py

# --- __init__.py: control 配下のものは新パッケージ用に持ち込み/新規作成 ---
git mv tests/pcb_assembly/control/pasting/__init__.py tests/pcb_assembly/pasting/__init__.py
# tests/pcb_assembly/posctrl/__init__.py は新規（空）で作成
# 残る control 配下 __init__.py を削除
git rm tests/pcb_assembly/control/adjust/__init__.py
git rm tests/pcb_assembly/control/__init__.py
rmdir tests/pcb_assembly/control/adjust tests/pcb_assembly/control/pasting tests/pcb_assembly/control 2>/dev/null || true
```

注: `tests/pcb_assembly/posctrl/__init__.py` は空で新規作成（既存テスト init はすべて空）。
`__pycache__` は無視（`make clean` で消える / .gitignore 対象）。

## import 書き換え対応表（ファイル別・行別）

### src 本体（移動先ファイル内の相互参照）

| ファイル | 旧 import | 新 import |
|---|---|---|
| `posctrl/setup.py` L16-20 | `from pcb_assembly.control.adjust import (BoardTransformMeasurer, OffsetTransformMeasurer, XYPositionAdjustor)` | `from pcb_assembly.posctrl.board import BoardTransformMeasurer`<br>`from pcb_assembly.posctrl.offset import OffsetTransformMeasurer`<br>`from pcb_assembly.posctrl.position import XYPositionAdjustor` |
| `posctrl/tour.py` L6 | `from pcb_assembly.control.setup import BoardCalibrationResult` | `from pcb_assembly.posctrl.setup import BoardCalibrationResult` |
| `pasting/height.py` L7 | `from pcb_assembly.control.probe import ProbeExecutor` | `from pcb_assembly.pasting.probe import ProbeExecutor` |
| `pasting/applicator.py` L11 | `from pcb_assembly.control.pasting.fill_path import build_paste_fill_path` | `from pcb_assembly.pasting.fill_path import build_paste_fill_path` |
| `pasting/loading.py` L5 | `from pcb_assembly.control.pasting.applicator import PasteApplicator` | `from pcb_assembly.pasting.applicator import PasteApplicator` |

注: setup.py の旧 import は `adjust/__init__.py` 経由の集約 import だったが、`adjust/__init__.py` は廃止するため
**サブモジュール直 import に展開**する（上表どおり）。posctrl の 3 クラスは別ファイルにあるため 3 行に分かれる。

### src/scripts（plan-implementer 担当）

| ファイル | 旧 import | 新 import |
|---|---|---|
| `scripts/dev/fill_path_simulate.py` L30 | `from pcb_assembly.control.pasting.fill_path import build_paste_fill_path` | `from pcb_assembly.pasting.fill_path import build_paste_fill_path` |
| `scripts/pasting/paste_flow_calibration.py` L21-25 | `from pcb_assembly.control.pasting import (FlowCalibration, PasteApplicator, interactive_loading)` | `from pcb_assembly.pasting import (FlowCalibration, PasteApplicator, interactive_loading)` |
| `scripts/pasting/paste_loading.py` L11 | `from pcb_assembly.control.pasting import PasteApplicator, interactive_loading` | `from pcb_assembly.pasting import PasteApplicator, interactive_loading` |
| `scripts/pasting/paste_solder.py` L20 | `from pcb_assembly.control.adjust import HeightPlaneMeasurer` | `from pcb_assembly.pasting import HeightPlaneMeasurer` |
| `scripts/pasting/paste_solder.py` L21 | `from pcb_assembly.control.pasting import PasteApplicator, interactive_loading` | `from pcb_assembly.pasting import PasteApplicator, interactive_loading` |
| `scripts/pasting/paste_solder.py` L22 | `from pcb_assembly.control.probe import ProbeExecutor` | `from pcb_assembly.pasting import ProbeExecutor`（または `pcb_assembly.pasting.probe`） |
| `scripts/pasting/paste_solder.py` L23-27 | `from pcb_assembly.control.setup import (BoardCalibrationResult, machine_session, setup_board_calibration)` | `from pcb_assembly.posctrl import (BoardCalibrationResult, machine_session, setup_board_calibration)`（または `pcb_assembly.posctrl.setup`） |
| `scripts/pasting/pasting_height_plane.py` L18 | `from pcb_assembly.control.adjust import HeightPlaneMeasurer` | `from pcb_assembly.pasting import HeightPlaneMeasurer` |
| `scripts/pasting/pasting_height_plane.py` L19 | `from pcb_assembly.control.probe import ProbeExecutor` | `from pcb_assembly.pasting import ProbeExecutor` |
| `scripts/pasting/pasting_height_plane.py` L20 | `from pcb_assembly.control.setup import machine_session, setup_board_calibration` | `from pcb_assembly.posctrl import machine_session, setup_board_calibration` |
| `scripts/pasting/pasting_toolhead_offset.py` L28 | `from pcb_assembly.control.adjust import XYPositionAdjustor` | `from pcb_assembly.posctrl import XYPositionAdjustor` |
| `scripts/pasting/pasting_toolhead_offset.py` L29-33 | `from pcb_assembly.control.pasting import (PasteApplicator, ToolheadOffsetResult, interactive_loading)` | `from pcb_assembly.pasting import (PasteApplicator, ToolheadOffsetResult, interactive_loading)` |
| `scripts/pasting/pasting_toolhead_offset.py` L34 | `from pcb_assembly.control.probe import ProbeExecutor` | `from pcb_assembly.pasting import ProbeExecutor` |
| `scripts/pasting/pasting_toolhead_offset.py` L35-39 | `from pcb_assembly.control.setup import (OffsetObserver, machine_session, setup_board_calibration)` | `from pcb_assembly.posctrl import (OffsetObserver, machine_session, setup_board_calibration)` |
| `scripts/posctrl/board_tour.py` L24-27 | `from pcb_assembly.control.setup import (machine_session, setup_board_calibration)` | `from pcb_assembly.posctrl import (machine_session, setup_board_calibration)` |
| `scripts/posctrl/board_tour.py` L28 | `from pcb_assembly.control.tour import display_at_point, wait_for_keypress` | `from pcb_assembly.posctrl import display_at_point, wait_for_keypress` |
| `scripts/posctrl/orthogonality_test.py` L19 | `from pcb_assembly.control.setup import machine_session, setup_board_calibration` | `from pcb_assembly.posctrl import machine_session, setup_board_calibration` |
| `scripts/posctrl/orthogonality_test.py` L20 | `from pcb_assembly.control.tour import interactive_display_at_point` | `from pcb_assembly.posctrl import interactive_display_at_point` |

import 形式の方針（plan-implementer に一任）:
- scripts は **`__init__` 集約 API 経由**（`from pcb_assembly.pasting import ...` / `from pcb_assembly.posctrl import ...`）に
  統一すると import 行数が減り見通しが良い。上表の「新 import」列はこの方針で記載。
- ただし既存スタイル（サブモジュール直 import）に合わせたい場合は `pcb_assembly.posctrl.setup` 等でも可。
  **どちらかに統一**すること。混在させない。

### tests（spec-test-author 担当）

| ファイル | 旧 | 新 |
|---|---|---|
| `tests/.../posctrl/test_setup.py` L7 | `from pcb_assembly.control.setup import OffsetObserver, machine_session` | `from pcb_assembly.posctrl.setup import OffsetObserver, machine_session` |
| `tests/.../posctrl/test_setup.py` L30 | `mocker.patch("pcb_assembly.control.setup.cv2")` | `mocker.patch("pcb_assembly.posctrl.setup.cv2")` |
| `tests/.../posctrl/test_setup.py` L86 | `mocker.patch("pcb_assembly.control.setup.cv2.destroyAllWindows")` | `mocker.patch("pcb_assembly.posctrl.setup.cv2.destroyAllWindows")` |
| `tests/.../posctrl/test_setup.py` L97 | 同上 | 同上 |
| `tests/.../pasting/test_height.py` L6 | `from pcb_assembly.control.adjust import HeightPlaneMeasurer` | `from pcb_assembly.pasting.height import HeightPlaneMeasurer`（または `pcb_assembly.pasting`） |
| `tests/.../pasting/test_applicator.py` L8 | `from pcb_assembly.control.pasting import PasteApplicator` | `from pcb_assembly.pasting import PasteApplicator` |
| `tests/.../pasting/test_calibration.py` L6 | `from pcb_assembly.control.pasting.calibration import FlowCalibration` | `from pcb_assembly.pasting.calibration import FlowCalibration` |
| `tests/.../pasting/test_fill_path.py` L7 | `from pcb_assembly.control.pasting.fill_path import (...)` | `from pcb_assembly.pasting.fill_path import (...)` |
| `tests/.../pasting/test_loading.py` L8 | `from pcb_assembly.control.pasting.loading import interactive_loading` | `from pcb_assembly.pasting.loading import interactive_loading` |
| `tests/.../pasting/test_toolhead_offset.py` L5 | `from pcb_assembly.control.pasting import ToolheadOffsetResult` | `from pcb_assembly.pasting import ToolheadOffsetResult` |

注: test の `mocker.patch` 文字列は import パスと同じく書き換え対象（patch 対象モジュールが移動するため）。
`patch("pcb_assembly.posctrl.setup.cv2")` のように **モジュール実体の新パス**を指す必要がある。
`@mark_hardware` / `skip_if_no_*` デコレータは行も中身も変更しない（移動のみ）。

## 実装ステップ

### plan-implementer 用（src 担当）

1. ブランチ `refactor/20260527/phase2-control-decompose` 上で作業（既に作成済み）。
2. src の `git mv` コマンド列を実行（posctrl / pasting ディレクトリ作成 → mv → control 残骸の `git rm`）。
3. 移動先ファイル内の相互参照 import を新パスへ書き換え（src 本体対応表 5 件）。
4. `posctrl/__init__.py` を新規 Write（設計どおり）。`pasting/__init__.py` を Edit（height/probe 追加）。
5. `pnp/__init__.py` を空で新規作成。
6. scripts の import を対応表どおり書き換え（9 ファイル）。import 形式（集約 / 直）を 1 つに統一。
7. `make format && make type` で静的検証。control 残参照ゼロを `grep -rn "pcb_assembly.control" src/` で確認。

### spec-test-author 用（tests 担当 — 並列実行可能）

公開 IF（クラス名・関数シグネチャ・新モジュールパス）は本計画で確定済みのため、src 実装と並列で進められる。
ただし **テスト実行（`make test`）は src 完了後**。コード変更（mv + import 追従）自体は独立に可能。

1. tests の `git mv` コマンド列を実行（posctrl / pasting ディレクトリ作成 → mv → control 残骸の `git rm`）。
2. `tests/pcb_assembly/posctrl/__init__.py` を空で新規作成（pasting 側は mv で持ち込み済み）。
3. 各 test の import パスと `mocker.patch` 文字列を対応表どおり書き換え（test_setup の patch 3 箇所含む）。
4. ロジック・アサーション・`@mark_hardware` は一切変更しない（移動と import 追従のみ）。
5. control 残参照ゼロを `grep -rn "pcb_assembly.control" tests/` で確認。

## テスト観点

本フェーズは「移動 + import 追従」であり新規ロジックは無い。新規テストは書かない。
既存テストが移動後も同じ振る舞いで通ることが成功条件。

- 正常系: `make test-no-hardware` で移動後の全テストが収集・通過する（import エラーなし）。
- 正常系: `pasting.height.ProbeExecutor` 相互参照、`pasting.applicator → fill_path`、`pasting.loading → applicator`、
  `posctrl.setup → board/offset/position`、`posctrl.tour → setup` が解決する。
- 異常系（回帰検出）: `grep -rn "pcb_assembly.control" src/ tests/` の結果が空であること。
  残っていれば import 漏れ → import エラーで `make test` が落ちる。
- エッジケース: `make type`（pyright）が新パスで型解決できること（特に `__init__` 集約 export の `__all__` 整合）。
- エッジケース: ハードウェアテスト（`test_height.py` 等が `@mark_hardware` を持つ場合）は
  `make test-no-hardware` でスキップされる構造が維持されていること。

## 検証チェックリスト（make run）

```bash
cd /home/gop/pcb-assembly
grep -rn "pcb_assembly.control" src/ tests/    # → 出力ゼロであること
make format                                     # ruff / docformatter（import 整列含む）
make type                                        # pyright が新パスで通る
make test-no-hardware                            # ハードウェア無しテスト通過
make test                                        # 実機ありフル（実機環境で）
# 上記をまとめて: make run（format → test → type）
```

合格条件: `grep` 結果ゼロ + `make run` 全通過。

## 想定リスク・トレードオフ

1. **`__init__` 集約 import vs サブモジュール直 import の方針**: scripts/tests の新 import を
   集約 API 経由にするか直 import にするかは見通しの好みの問題。本計画は集約 API 推奨だが、
   plan-implementer が 1 つに統一すれば可。混在だけは避ける。
2. **`mocker.patch` 文字列の見落とし**: `test_setup.py` の 3 箇所の patch 文字列は import 文と別に
   存在するため grep 追従が必須。書き換え漏れると patch 対象が存在せず AttributeError。
3. **`git mv` と `__init__` の扱い**: `adjust/__init__.py` は posctrl にそのまま使えない（中身が異なる）ため
   `git rm` + 新規作成とした。履歴追跡は失われるが内容が大きく変わるため許容。`pasting/__init__.py` は
   `git mv` で持ち込み Edit する（履歴維持）。
4. **空ディレクトリの残留**: `git mv` 後に `control/`, `control/adjust/`, `control/pasting/` が空で残る。
   git は空ディレクトリを追跡しないため `git rm` 完了後は自動消滅するが、`__pycache__` が残ると `rmdir` が失敗する。
   `make clean` または手動 `rm -rf __pycache__` を先に行う。
5. **並列実行時の衝突**: src 担当と tests 担当は disjoint なファイル群（src/ と tests/）を触るため
   ファイル衝突は無い。ただし両者とも `control/` ディレクトリ削除に絡むため、ディレクトリ削除（rmdir）は
   それぞれ自分の管轄（src 側 / tests 側）のみ行う。
6. **Phase 3 への影響**: 本フェーズで `pcb_assembly` 名は維持。Phase 3 のパッケージリネーム時に
   posctrl/pasting/pnp も追従するが、それは別フェーズ。本計画では `pcb_assembly.*` のままに保つ。

## 参照

- 全体計画: `/home/gop/.claude/plans/claude-src-scripts-paste-solder-py-recursive-candle.md`
- カプセル化・外科的変更: skill `refactor-conventions`
- tests ミラー・@mark_hardware: skill `testing-strategy`, `hardware-test`
- 並列起動: skill `agent-team-startup`, `maximize-parallels`
- 移動元: `src/pcb_assembly/control/`（setup.py, tour.py, probe.py, adjust/*, pasting/*）
- 移動先: `src/pcb_assembly/posctrl/`, `src/pcb_assembly/pasting/`, `src/pcb_assembly/pnp/`
