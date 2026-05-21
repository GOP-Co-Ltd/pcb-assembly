# エージェント間共有メモリ

マルチエージェントでタスクを進める際に、各エージェントが残す中間メモを格納する。
すべて git 管理対象とし、履歴として残す。

## ディレクトリ構成

```
agents/
├── implementation-planner/  # 計画書
├── plan-implementer/        # 実装ノート、計画外判断ログ、IF変更通知
├── code-simplifier/         # 簡素化ノート
└── docs-keeper/             # ドキュメント整備ノート
```

## 利用ルール

- **各 agent は自身のフォルダ配下にのみ書き込む**（混線回避）
- ファイル命名：`<task-slug>.md`（例：`height-plane-refactor.md`）
- 並列実装時は `<task-slug>-<instance>.md` で識別（例：`height-plane-refactor-geometry.md`）
- 後続 agent は前段 agent のフォルダを必ず読んでから着手する
- タスク完了後の保持・削除はユーザー判断（デフォルトは保持）

## エージェント間の典型フロー

```
implementation-planner/<task>.md  ← planner が計画書を書く
        ↓
plan-implementer/<task>.md         ← implementer が実装ノートを残す
        ↓
code-simplifier/<task>.md          ← simplifier が簡素化ノートを残す
        ↓
docs-keeper/<task>.md              ← docs-keeper がドキュメント整備ノートを残す
```

詳細手順は skill `agent-team-startup` 参照。
