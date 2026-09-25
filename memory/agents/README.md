# エージェント間共有メモリ

マルチエージェントでタスクを進める際に、各エージェントが残す中間メモを格納する。
すべて git 管理対象とし、履歴として残す。

## ディレクトリ構成

```
agents/
├── orchestrator/            # 委譲判断・レビュー裁定・計画外決定のログ
├── implementation-planner/  # 計画書
├── spec-test-author/        # テスト一覧、仕様根拠対応、実装側修正要求
├── plan-implementer/        # 実装ノート、計画外判断ログ、IF変更通知
├── code-reviewer/           # レビュー結果（verdict、must-fix / should-fix / nit）
├── code-simplifier/         # 簡素化ノート + ドキュメント同期の記録
├── doc-teacher/             # 文書の理解度テスト（問題・模範解答・ラウンド記録）。skill doc-teacher-student
└── docs-keeper/             # 【廃止】過去ノートの保管のみ。責務は code-simplifier に統合済み
```

## 利用ルール

- **各 agent は自身のフォルダ配下にのみ書き込む**（混線回避）
- ファイル命名：`<task-slug>.md`（例：`height-plane-refactor.md`）
- 並列実装時は `<task-slug>-<instance>.md` で識別（例：`height-plane-refactor-geometry.md`）
- 後続 agent は前段 agent のフォルダを読んでから着手する
- タスク完了後の保持・削除はユーザー判断（デフォルトは保持）
- `docs-keeper/` には新規に書き込まない（廃止済み）
- ソロ（委譲せずロールを切り替えて進める skill `solo-dev-cycle`）の場合はロールごとに分けず、セッションのエージェント（既定 `orchestrator/`）配下の 1 ファイルに段階ごと追記する

## エージェント間の典型フロー

```
implementation-planner/<task>.md  ← planner が計画書を書く
        ↓
spec-test-author/<task>.md         ← spec-test-author がテスト一覧と仕様根拠を残す（仕様 first フローで使用）
        ↓
plan-implementer/<task>.md         ← implementer が実装ノートを残す
        ↓
code-reviewer/<task>.md            ← reviewer が verdict と指摘を残す（must-fix は implementer に差し戻し）
        ↓
code-simplifier/<task>.md          ← simplifier が簡素化とドキュメント同期のノートを残す

orchestrator/<task>.md             ← 全体を通した委譲判断・レビュー裁定のログ（orchestrator が随時更新）
```

`spec-test-author` は任意ステップ。仕様が明確で TDD 的に進めたいときに挟み、`plan-implementer` と並列実行できる。

詳細手順は skill `agent-team-startup` 参照。
