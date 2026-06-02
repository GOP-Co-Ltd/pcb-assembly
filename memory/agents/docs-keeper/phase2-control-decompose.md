# Phase 2: control 解体 → posctrl / pasting 分離（docs-keeper）

ブランチ: `refactor/20260527/phase2-control-decompose`
スコープ: ドキュメント・README のみ。コード（src/tests）は触っていない。コミットは Claude main が実施。
パッケージ名は `pcb_assembly` のまま維持（リネームは Phase 3）。

## 修正したドキュメント

- `CLAUDE.md`「主要モジュール構成」: 消滅した `control/` の 1 行を削除し、`posctrl/` / `pasting/` / `pnp/` の
  3 行に置換。各 1 行・既存の terse スタイルを踏襲。`pnp/` は「将来用」を明記。
  - `posctrl/` — Board/オフセットの位置合わせ共通制御（setup, tour, board/position/offset 調整）
  - `pasting/` — ペースト塗布専用ロジック（applicator, calibration, fill_path, loading, probe, height 等）
  - `pnp/` — Pick and Place 用の名前空間（将来用）

## 新規作成した README

既存の hal/README.md・vision/README.md の粒度（タイトル + 1 行役割 + 短い箇条書き + 1 行の補足）に合わせた。

- `src/pcb_assembly/posctrl/README.md`: 位置合わせ共通制御。pasting/pnp 双方から使う旨を補足。
- `src/pcb_assembly/pasting/README.md`: ペースト塗布専用。位置合わせは posctrl を使う旨を補足。
- `src/pcb_assembly/pnp/README.md`: 空パッケージ。「Pick and Place 用の名前空間。実装は未着手。」の 2 行のみ。

## 判断ログ

- **pnp に README を置いた**: geometry/pcb（実体ある非空パッケージ）には README が無く、README 慣習は全パッケージ網羅ではない。
  ただし pnp は空 `__init__.py` のみで、README が無いと「作りかけの取り残し」に見える。空であることと意図（将来用）を
  最小 2 行で明示する方が有益と判断し設置。投機的な構造説明は書いていない。
- **docstring 追従なし**: posctrl/pasting 配下の各 .py のモジュール docstring に "control" 表記は無し
  （`grep -rn "control" --include="*.py"` 一致ゼロ）。各 docstring はファイル自身の責務を述べており陳腐化なし。修正不要。
- **CLAUDE.md「プロジェクト概要」の "制御ロジック"（L56）は据え置き**: `control` という固有名を含まない汎用記述で、
  posctrl/pasting も依然「制御ロジック」に含まれ陳腐化していない。外科的変更の原則により非変更。
- **CLAUDE.md「参照先マップ」は非変更**: control を参照する箇所は無かった（参照先は memory/ と .claude/skills/ のみ）。

## あえて触らなかったドキュメント・理由

- `.claude/skills/agent-team-startup/SKILL.md` L99 の `control` 言及:
  並列エージェント割当の**例示**（「`geometry` 配下を 1 セット、`control` 配下を別セットで」）であり、
  パッケージ構成のマップではない。構造記述の追従対象外。スコープ外（過剰修正回避）として非変更。
- ルート `README.md`: control への言及なし（セットアップ手順のみ）。変更不要。
- `docs/` ディレクトリは存在しない。
- `memory/` 配下の各ノート: 規約・中間メモであり構造記述ではないため対象外（指示どおり）。

## 後続に引き継ぐ事項

- Phase 3 の `pcb_assembly` → `pcbasm` リネーム時、本フェーズで書いた CLAUDE.md の `src/pcb_assembly/` 表記と
  新規 README（posctrl/pasting/pnp）内のパッケージ名参照を追従させること。
- ディスク上に空の `src/pcb_assembly/control/`（`adjust/`・`pasting/`・`__pycache__` のみ、追跡ファイルゼロ）が残存。
  ドキュメント観点では無害。plan-implementer ノート記載どおりコミットで git 上は消滅する。
