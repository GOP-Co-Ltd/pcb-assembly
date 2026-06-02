# Phase 1: scripts 整理 — test import パス追従

ブランチ: `refactor/20260527/phase1-scripts-reorg`
担当: spec-test-author（`tests/` のみ。`src/` 不可触）
根拠: `memory/agents/implementation-planner/phase1-scripts-reorg.md` セクション B「唯一の破壊ポイント」/ 実装ステップ 4

## 作業内容

`src/scripts/generate_grid_pcb.py` → `src/scripts/dev/generate_grid_pcb.py` 移動（plan-implementer 実施）に
伴い、`tests/scripts/test_generate_grid_pcb.py` の import パス文字列のみを追従させた。
**アサーション・テストロジック・テストクラス構成・pcbnew モック方針は一切変更していない。**

## 書き換えた箇所（出現実数: 10 行）

計画書の見積りは「import 10 箇所 + sys.modules キー 1 箇所」だったが、
実ファイルの出現実数は **10 行**（grep で全件特定後に書き換え）。内訳:

| 行 | 旧 | 新 |
|---|---|---|
| 50 | `if "scripts.generate_grid_pcb" in sys.modules:` | `if "scripts.dev.generate_grid_pcb" in sys.modules:` |
| 51 | `del sys.modules["scripts.generate_grid_pcb"]` | `del sys.modules["scripts.dev.generate_grid_pcb"]` |
| 59 | `from scripts.generate_grid_pcb import generate_grid_pcb` | `from scripts.dev.generate_grid_pcb import generate_grid_pcb` |
| 74 | 同上 | 同上 |
| 85 | 同上 | 同上 |
| 96 | 同上 | 同上 |
| 105 | 同上 | 同上 |
| 126 | 同上 | 同上 |
| 143 | `from scripts.generate_grid_pcb import main` | `from scripts.dev.generate_grid_pcb import main` |
| 153 | 同上 | 同上 |

- `generate_grid_pcb` import: 6 行（59/74/85/96/105/126）
- `main` import: 2 行（143/153）
- `sys.modules` 参照: 2 行（50 の `in` チェック / 51 の `del` キー）

計画書の「8 import 文 + 2 main + 1 sys.modules」という表現は概数。
実際は import 文 8 行 + sys.modules 2 行 = 計 10 行（`in`/`del` を別行とカウント）。

## 検証

- `grep -rnE "scripts\.generate_grid_pcb" ... | grep -v "scripts\.dev\.generate_grid_pcb"` → **残存なし (OK)**。
  旧パス（`scripts.dev.` が前置されていない `scripts.generate_grid_pcb`）はゼロ。
- `make format`（pre-commit 全フック）→ **全 Passed**。ruff / ruff-format / docformatter とも
  本ファイルへの変更なし（既に整形済みの状態）。

## 注意点 / 引き継ぎ

- **`make test-no-hardware` は spec-test-author 側では実行していない**（計画書ステップ 5 の指示通り）。
  src のファイル移動（plan-implementer のステップ 1）と本 test 追従が両方揃わないと collection error に
  なるため、統合検証は Claude main が両者完了後に実行する。
- 依存前提: `scripts` / `scripts.dev` は `__init__.py` を持たない namespace package。
  plan-implementer が `src/scripts/dev/` に `__init__.py` を**置かない**（計画書「完全対応表」L82）こと
  が、`from scripts.dev.generate_grid_pcb import ...` の解決前提。置いてしまうと namespace 解決と齟齬が
  出る可能性があるため、統合検証で import 解決を必ず確認すること。
- pcbnew モック（`monkeypatch.setitem(sys.modules, "pcbnew", ...)`）は 3rd-party 表面だが、
  generate_grid_pcb は pcbnew を直接叩く実行スクリプトであり、本作業ではモック方針に手を加えていない
  （パス追従のみのスコープ）。モック方針の是非は本タスクの範囲外。

## コミット

未実施（指示通り）。
