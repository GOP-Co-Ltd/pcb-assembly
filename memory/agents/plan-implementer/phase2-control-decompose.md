# Phase 2: control 解体 → posctrl / pasting 分離（plan-implementer / src 担当）

ブランチ: `refactor/20260527/phase2-control-decompose`
スコープ: `src/` のみ（tests/ は spec-test-author が並行担当）。コミットは Claude main が統合検証後に実施。

## 実施した git mv 結果（src）

計画書「### src」のコマンド列を順に実行。すべて rename として追跡された。

posctrl 新設:
- control/setup.py → posctrl/setup.py
- control/tour.py → posctrl/tour.py
- control/adjust/board.py → posctrl/board.py
- control/adjust/position.py → posctrl/position.py
- control/adjust/offset.py → posctrl/offset.py

pasting 新設:
- control/pasting/{applicator,calibration,fill_path,loading,toolhead_offset,__init__}.py → pasting/同名
- control/probe.py → pasting/probe.py
- control/adjust/height.py → pasting/height.py

control 残骸削除（git rm）:
- control/__init__.py（0 byte）
- control/adjust/__init__.py
- **control/README.md**（計画外。下記「計画外の判断」参照）

新規作成:
- posctrl/__init__.py（新規 Write、計画設計どおり）
- pnp/__init__.py（空）

## 計画外の判断ログ

1. **control/README.md の git rm（計画に記載なし）**
   - 計画の `git rm` は `control/__init__.py` / `control/adjust/__init__.py` のみを対象とし、
     `control/README.md`（内容: 「Controlモジュール — 実際に機器を制御する処理を記述します。」）に言及がなかった。
   - control パッケージを完全解体する本フェーズの意図（「control 残骸の削除」「control ディレクトリは git では自動消滅」）に照らすと、
     README を残すと control/ が git 上に生き残り、計画意図と矛盾する。CLAUDE.md 原則3（自分の変更で生じた orphan は消す）にも合致するため `git rm` した。
   - 新パッケージ（posctrl/pasting/pnp）への README 新設は本フェーズスコープ外（移動 + import のみ）のため行っていない。必要なら docs-keeper フェーズで検討。

2. **fill_path_simulate.py のみサブモジュール直 import を維持**
   - scripts は原則「集約 API 経由」に統一したが、`build_paste_fill_path` は `pasting/__init__.py` の公開 API（__all__）に含まれない（計画の pasting 新設計どおり）。
   - そのため計画対応表どおり `from pcb_assembly.pasting.fill_path import build_paste_fill_path`（直 import）のままとした。集約 API に存在しないシンボルなので、これは「混在」ではなく公開面の必然。

## import 形式の統一方針

- scripts は **集約 API 経由**に統一（`from pcb_assembly.pasting import ...` / `from pcb_assembly.posctrl import ...`）。
- 同一パッケージへの複数旧 import は 1 ブロックに統合（ruff の isort で member ソート確定）。
  - 例 paste_solder.py: control.adjust/pasting/probe の 3 行 → `from pcb_assembly.pasting import (HeightPlaneMeasurer, PasteApplicator, ProbeExecutor, interactive_loading)` の 1 ブロック。
- 例外は上記 fill_path_simulate.py のみ（公開 API 非搭載シンボルのため直 import）。
- src 本体（移動先の相互参照）は計画対応表どおり: posctrl/setup.py は board/offset/position の 3 行直 import に展開、他はサブモジュール直 import で新パスへ置換。

## __init__ 設計の確定

- posctrl/__init__.py: 計画設計どおり 10 シンボル（board/offset/position + setup の 4 + tour の 3）。private `_BoardPointProber` 等は非 export。__all__ と実シンボルの存在を grep で確認済み。
- pasting/__init__.py: 既存 4 シンボルに HeightPlaneMeasurer, ProbeExecutor を追加（計 6）。__all__ 更新。
- pnp/__init__.py: 空。

## 他 implementer への IF 変更通知（並列時）

- なし。公開シンボル名・シグネチャは一切変更していない。計画書のシグネチャ案から逸脱なし。
- 公開モジュールパスは計画どおり: `pcb_assembly.posctrl.*` / `pcb_assembly.pasting.*`。
  spec-test-author の対応表（test import / mocker.patch 文字列）と整合する新パスを提供済み。

## 既知の制約・残課題

- `src/pcb_assembly/control/` ディレクトリがディスク上に残存（中身は stale `.pyc` のみ）。
  指示により `make clean` / 広域 `__pycache__` 削除は実施せず（tests 並行作業中）。
  追跡ファイルはゼロ（`git ls-files src/pcb_assembly/control/` が空）。コミット後 git 上は消滅、ディスクの空ディレクトリは無害。
- `git status` 上、削除した `src/.../control/__init__.py`（0 byte）が
  spec-test-author が新規作成した `tests/.../pasting/__init__.py`（同じく空）と rename ペアとして表示される
  （`R src/pcb_assembly/control/__init__.py -> tests/pcb_assembly/pasting/__init__.py`）。
  これは git の rename 検出が同一内容（空ファイル）を跨いでマッチした表示上のアーティファクトで、実害なし。
  両者を一括コミットすれば「control/__init__.py 削除」と「tests/pasting/__init__.py 新規」として正しく解決する。触っていない。

## 検証結果

- `grep -rn "pcb_assembly.control" src/`: 出力ゼロ（exit 1）。py/md/cfg/toml の広域 grep も一致なし。
- `make format`: pass（初回 ruff が import 統合/整列を 8 件 auto-fix → 再実行で全 hook Passed）。
- `make type`: **0 errors**, 2 warnings。warning は 2 件とも `tests/pcb_assembly/pasting/test_fill_path.py` の
  `reportPrivateUsage`（`_generate_linear_path` / `_generate_spiral_path` の private 使用）。
  これは tests/ 側（spec-test-author 管轄）の既存事象で、本移動とは無関係・error ではない。src 側に型エラーなし。
- `make test` / `make test-no-hardware`: 指示により未実行（spec-test-author の tests 追従と並行のため、統合検証は Claude main が両者完了後に実施）。
