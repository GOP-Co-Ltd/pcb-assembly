# Custom Agent チームの補助資料

`agent-team-startup` の並列化パターンと failure mode をまとめる。
必要になった時点で参照する。

## 仕様テストと実装の並列化

公開 IF がシグネチャレベルで確定し、担当範囲が disjoint なら、
`spec-test-author` と `plan-implementer` を同時に起動できる。

```text
implementation-planner
        ↓
   ┌────┴────┐
   ↓         ↓
spec-test-author  plan-implementer
   ↓         ↓
   └────┬────┘
        ↓
format / type / test-no-hardware
        ↓
code-reviewer → code-simplifier → 必要なら再 review
```

前提:

- planner が関数名、引数、戻り値型まで確定している
- `spec-test-author` は `tests/`、`plan-implementer` は `src/` を所有する
- 共有 fixture の所有者を一方に固定する
- 同一 hardware resource を使わない。agent は実機テストを実行しない
- 各 worker に、他の作業者の変更を revert しないよう明記する

探索的な実装、未確定 IF、共有 fixture の同時編集がある場合は直列化する。

## モジュール単位の並列化

独立した module ごとに担当 agent を分ける場合は、書き込み範囲と中間メモも分離する。

- `memory/agents/plan-implementer/<task>-<instance>.md`
- `memory/agents/spec-test-author/<task>-<instance>.md`
- `memory/agents/code-reviewer/<task>-<instance>.md`
- `memory/agents/code-simplifier/<task>-<instance>.md`

他の担当へ影響する IF 変更が必要になったら並列作業を止め、planner とメイン agent で
公開 IF を再確定する。

## Failure mode

| 状況                                          | 対応                                                  |
| --------------------------------------------- | ----------------------------------------------------- |
| planner の計画が曖昧                          | 確認事項をメイン agent がユーザーへ中継する           |
| test author が仕様の欠落を発見                | planner へ戻して計画を更新する                        |
| implementer が仕様テストに異議                | テストを編集せず、仕様根拠を test author と再確認する |
| reviewer が must-fix を報告                   | implementer に根拠とともに差し戻す                    |
| reviewer と implementer の往復が 2 回を超える | メイン agent が裁定し、必要ならユーザーへ確認する     |
| simplifier が大きく書き換えた                 | reviewer が再確認する                                 |
| 同じファイルの編集が必要になった              | 並列化を止め、所有者を一方に固定する                  |

並列成果を合わせた後は、メイン agent が次を実行する。

```bash
make format
make type
make test-no-hardware
```
