# Phase 2: control 解体 — tests 追従（spec-test-author）

ブランチ: `refactor/20260527/phase2-control-decompose`
スコープ: `tests/` のみ（`src/` は plan-implementer 担当、未着手でも独立に完了可）。
方針: **移動 + import/patch パス追従のみ**。アサーション・テストロジック・テストクラス構成・
`@mark_hardware`/`skip_if_no_*` は一切変更していない。

## git mv 結果

新構造（`src/pcb_assembly/posctrl` `pasting` をミラー）:

```
tests/pcb_assembly/posctrl/__init__.py        # 空・新規（旧 control/adjust/__init__.py が空のため git は rename 検出）
tests/pcb_assembly/posctrl/test_setup.py      # ← control/test_setup.py
tests/pcb_assembly/pasting/__init__.py        # ← control/pasting/__init__.py（git mv 持ち込み・空）
tests/pcb_assembly/pasting/test_height.py     # ← control/adjust/test_height.py
tests/pcb_assembly/pasting/test_applicator.py # ← control/pasting/test_applicator.py
tests/pcb_assembly/pasting/test_calibration.py
tests/pcb_assembly/pasting/test_fill_path.py
tests/pcb_assembly/pasting/test_loading.py
tests/pcb_assembly/pasting/test_toolhead_offset.py
```

削除: `tests/pcb_assembly/control/__init__.py`, `control/adjust/__init__.py`（git rm）。
`tests/pcb_assembly/control/` ディレクトリは __pycache__ 除去後 rmdir 済み（完全消滅）。
`make clean` は src 並行作業中のため**未実行**（指示どおり）。

注: 旧 `control/adjust/__init__.py`（空）→ `posctrl/__init__.py` を git が rename として記録。
中身は計画どおり空ファイルで一致しており実体は問題なし。

## 書き換えた import / patch 一覧

| ファイル | 旧 | 新 |
|---|---|---|
| posctrl/test_setup.py L7 | `from pcb_assembly.control.setup import OffsetObserver, machine_session` | `from pcb_assembly.posctrl.setup import OffsetObserver, machine_session` |
| pasting/test_height.py | `from pcb_assembly.control.adjust import HeightPlaneMeasurer` | `from pcb_assembly.pasting.height import HeightPlaneMeasurer` |
| pasting/test_applicator.py | `from pcb_assembly.control.pasting import PasteApplicator` | `from pcb_assembly.pasting import PasteApplicator` |
| pasting/test_calibration.py | `from pcb_assembly.control.pasting.calibration import FlowCalibration` | `from pcb_assembly.pasting.calibration import FlowCalibration` |
| pasting/test_fill_path.py | `from pcb_assembly.control.pasting.fill_path import (...)` | `from pcb_assembly.pasting.fill_path import (...)` |
| pasting/test_loading.py | `from pcb_assembly.control.pasting.loading import interactive_loading` | `from pcb_assembly.pasting.loading import interactive_loading` |
| pasting/test_toolhead_offset.py | `from pcb_assembly.control.pasting import ToolheadOffsetResult` | `from pcb_assembly.pasting import ToolheadOffsetResult` |

import 形式は計画書「新」列に厳密追従（test_setup/calibration/fill_path/loading/height は
サブモジュール直 import、applicator/toolhead_offset は集約 API）。各ファイル最小変更を優先した。

### mocker.patch 文字列 3 箇所（最重要・import 文とは別に存在）

posctrl/test_setup.py 内:

| 位置 | 旧 | 新 |
|---|---|---|
| mock_cv2 fixture | `mocker.patch("pcb_assembly.control.setup.cv2")` | `mocker.patch("pcb_assembly.posctrl.setup.cv2")` |
| test_sends_m84_and_destroys_windows | `mocker.patch("pcb_assembly.control.setup.cv2.destroyAllWindows")` | `mocker.patch("pcb_assembly.posctrl.setup.cv2.destroyAllWindows")` |
| test_cleanup_on_exception | `mocker.patch("pcb_assembly.control.setup.cv2.destroyAllWindows")` | `mocker.patch("pcb_assembly.posctrl.setup.cv2.destroyAllWindows")` |

patch 対象はモジュール実体の新パス（`pcb_assembly.posctrl.setup.cv2`）を指す。

注（testing-strategy 観点）: この cv2 patch は本フェーズで新規導入したものではなく**既存テストの
そのままの移植**。3rd-party 表面のモックに相当するため将来 testing-strategy 準拠で見直し候補だが、
本フェーズのスコープ（移動 + パス追従のみ）外なので一切変更していない。
同様に test_fill_path.py が private `_generate_linear_path` / `_generate_spiral_path` を
import している点も既存どおり維持（リファクタ対象外）。

## grep 検証

```
grep -rn "pcb_assembly.control" tests/   → ZERO matches（import 文・patch 文字列とも）
```

`@mark_hardware` / `skip_if_no_*`: 移動対象ファイル群には元々存在せず（grep でも 0 件）、
追加・削除なし。

## format 検証

`make format` 通過（exit=0）。初回は ruff が import ブロックを再ソート（8 fix、import 並び順のみ。
アサーション・ロジックには非接触）。再実行で全フック Passed。

## 注意点・引き継ぎ

- `make test-no-hardware` / `make test` は**未実行**（src 移動未完了なら collection error。統合検証は
  両者完了後に Claude main が実施）。
- 期待: src 側が posctrl/pasting を計画どおり作成し `posctrl/__init__.py`（setup/tour/board/offset/position）
  と `pasting/__init__.py`（height/probe 追加）を整備すれば、本 tests の import は全解決するはず。
  - 特に `pasting/test_height.py` は `pcb_assembly.pasting.height.HeightPlaneMeasurer` を直 import するため
    src 側 `pasting/height.py` の存在が前提。
  - `pasting/test_applicator.py` / `test_toolhead_offset.py` は集約 `pcb_assembly.pasting` 経由なので
    src 側 `pasting/__init__.py` の `__all__`（PasteApplicator, ToolheadOffsetResult）整備が前提。
- コミットは未実施（指示どおり）。staged 済み。
```
