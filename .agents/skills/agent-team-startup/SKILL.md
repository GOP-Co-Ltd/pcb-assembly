---
name: agent-team-startup
description: implementation-planner、任意のspec-test-author、plan-implementer、code-reviewer、code-simplifierを使うCodex custom agentフローと並列化基準。ユーザーがエージェントや並列作業を明示したとき、またはcustom agent運用を依頼したときに参照する。
---

# Codex Custom Agent チームを運用する

ユーザーが sub-agent、エージェントチーム、並列 agent の利用を明示した場合に使う。
ユーザーの明示がない場合は custom agent を起動せず、メイン agent が作業する。

## Agent

- `implementation-planner`: 読み取り専用で要件と計画を確定する
- `spec-test-author`: `tests/` のみを担当し、仕様をテストへ翻訳する
- `plan-implementer`: 計画に基づき production code と必要なテストを実装する
- `code-reviewer`: 読み取り専用で仕様準拠、正しさ、テスト品質を review する
- `code-simplifier`: 公開 API を変えずに内部を簡素化し、関連文書を同期する

定義は `.codex/agents/*.toml`、中間メモは
`memory/agents/<agent-name>/<task-slug>.md` に置く。

## 標準サイクル

1. `implementation-planner` が現状、公開 IF、実装手順、テスト観点、リスクを整理する。
2. 公開 IF が確定していれば、必要に応じて `spec-test-author` を起動する。
3. `plan-implementer` が実装し、format、type、非実機 test を通す。
4. `code-reviewer` が verdict を出す。must-fix は `plan-implementer` へ戻す。
5. approve 後、`code-simplifier` が公開 IF を維持して簡素化し、関連文書を同期する。
6. 大きく書き換えた場合は `code-reviewer` が再確認する。

trivial な変更では planner、reviewer、simplifier を省略してよい。

## 並列化

次をすべて満たす場合だけ並列起動する。

- ユーザーが agent 利用または並列作業を明示している
- 担当ファイルと責務が disjoint
- 公開 IF がシグネチャレベルで確定している
- 一方の成果物が他方の入力にならない
- カメラ、Klipper、GPIO など同一ハードウェアを共有しない

典型例は、`spec-test-author` が `tests/pcbasm/`、`plan-implementer` が
`src/pcbasm/` を担当する仕様 first フロー。ただし implementer がテストを変更しない
こと、共有 fixture の所有者を一方に固定することを prompt に明記する。

## 起動時の指示

- task、入力資料、成功条件、書き込み範囲を具体的に渡す
- worker には「他の作業者の変更を revert しない」と明記する
- 同じファイルを複数 agent に割り当てない
- immediate blocker はメイン agent が処理し、sidecar task だけを委譲する
- 完了した agent の変更をメイン agent が review してから統合する
- pytest を直接実行させる場合は必ず `-m "not hardware"` を付ける

## 合流

```bash
make format
make type
make test-no-hardware
```

実機テストは agent が実行しない。実機確認はユーザーに委ねる。

詳細な並列化パターン、review の差し戻し、failure mode は
[reference.md](reference.md) を参照する。
