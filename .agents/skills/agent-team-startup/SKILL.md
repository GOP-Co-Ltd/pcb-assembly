---
name: agent-team-startup
description: implementation-planner、任意の spec-test-author、plan-implementer、code-simplifier、docs-keeper を使う Codex custom agent フローと並列化基準。ユーザーがエージェントや並列作業を明示したとき、または custom agent 運用を依頼したときに参照する。
---

# Codex Custom Agent チームを運用する

ユーザーが sub-agent、エージェントチーム、並列 agent の利用を明示した場合に使う。
ユーザーの明示がない場合は custom agent を起動せず、メイン agent が作業する。

## Agent

- `implementation-planner`: 読み取り専用で要件と計画を確定する
- `spec-test-author`: `tests/` のみを担当し、仕様をテストへ翻訳する
- `plan-implementer`: 計画に基づき production code と必要なテストを実装する
- `code-simplifier`: 公開 API を変えずに内部を簡素化する
- `docs-keeper`: README、仕様書、docstring の整合を最小変更で保つ

定義は `.codex/agents/*.toml`、中間メモは
`memory/agents/<agent-name>/<task-slug>.md` に置く。

## 標準サイクル

1. `implementation-planner` が現状、公開 IF、実装手順、テスト観点、リスクを整理する。
2. 公開 IF が確定していれば、必要に応じて `spec-test-author` を起動する。
3. `plan-implementer` が実装し、format、type、test を通す。
4. `code-simplifier` が公開 IF を維持したまま簡素化する。
5. `docs-keeper` がコードと文書の不整合を修正する。

trivial な変更では planner、simplifier、docs を省略してよい。

## 並列化

次をすべて満たす場合だけ並列起動する。

- ユーザーが agent 利用または並列作業を明示している
- 担当ファイルと責務が disjoint
- 公開 IF がシグネチャレベルで確定している
- 一方の成果物が他方の入力にならない
- カメラ、Klipper、GPIO など同一ハードウェアを共有しない

典型例は、`spec-test-author` が `tests/`、`plan-implementer` が
`src/` を担当する仕様 first フロー。ただし implementer がテストを変更しない
こと、共有 fixture の所有者を一方に固定することを prompt に明記する。

## 起動時の指示

- task、入力資料、成功条件、書き込み範囲を具体的に渡す
- worker には「他の作業者の変更を revert しない」と明記する
- 同じファイルを複数 agent に割り当てない
- すぐ解消が必要な blocker はメイン agent が処理し、並行して進められる付随タスクだけを委譲する
- 完了した agent の変更をメイン agent が review してから統合する

## 合流

```bash
make format
make type
make test-no-hardware
```

実機が利用可能で変更がハードウェアに関係する場合は、競合しないよう直列で
`make test` または対象 hardware test を実行する。
