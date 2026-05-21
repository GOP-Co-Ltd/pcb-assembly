---
name: agent-team-startup
description: implementation-planner → plan-implementer → code-simplifier → docs-keeper の標準フロー、および並列化（implementer/simplifierを並列）の判断基準。ユーザーが「エージェントチームで進めて」と言ったり、複数モジュールにまたがる中〜大規模変更を要求したときに参照する。
---

# エージェントチーム起動手順

ユーザーから「エージェントチームで」と指示があった場合、または中〜大規模な変更（複数モジュールにまたがる実装・リファクタリング）を行う場合の進行手順。

## 前提

- `settings.json` に `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` が設定済み（必須）
- 各 agent は `.claude/agents/` 配下に定義済み（implementation-planner, plan-implementer, code-simplifier, docs-keeper）
- 中間メモは `memory/agents/<agent-name>/<task-slug>.md` に書く（詳細は `memory/agents/README.md`）

## 標準サイクル

```
1. implementation-planner
   - 要件を仕様化し、計画書を memory/agents/implementation-planner/<task>.md に書く
   - コード変更はしない

2. plan-implementer
   - 計画書を読み、実装＋テスト＋型＋lint をグリーン化
   - 計画外の判断は memory/agents/plan-implementer/<task>.md に記録

3. code-simplifier
   - 公開IFを保持しつつ内部を簡素化
   - 簡素化内容を memory/agents/code-simplifier/<task>.md に記録
   - 2 ⇄ 3 は品質が十分になるまで繰り返してよい

4. docs-keeper
   - README/docstring を最小保守
   - 整備内容を memory/agents/docs-keeper/<task>.md に記録
```

## 並列化の判断基準

### 並列化してよいケース

- **独立したモジュール・機能** ごとに分割可能なとき
- **インターフェース境界が明確** で、互いの実装詳細に依存しないとき
- 例：`geometry` 配下と `control` 配下を別 implementer が並行実装

### 並列化を避けるケース

- インターフェース未確定で並行実装すると衝突するとき
- データフロー上で順序依存があるとき
- テスト基盤が共有されており同時編集で衝突するとき

### 並列実行の運用

- 並列 implementer は `memory/agents/plan-implementer/<task>-<instance>.md` 形式でファイル分離
- 他の implementer に影響するIF変更は同ファイルに「IF変更通知」セクションで明記
- 並列 simplifier も同様にファイル分離

## チェックポイント

| タイミング         | 確認事項                                                                     |
| ------------------ | ---------------------------------------------------------------------------- |
| planner 完了時     | 計画書に「公開IF案／実装ステップ／テスト観点／リスク」が揃っているか         |
| implementer 完了時 | `make format && make type && make test` がグリーン                           |
| simplifier 完了時  | 公開IF不変・全テスト通過・簡素化ノートに変更点列挙                           |
| 並列完了時         | **全並列分マージ後に1回 `make format && make type && make test` を必ず通す** |
| docs-keeper 完了時 | README/docstring が現状コードと整合                                          |

## Failure mode と対処

| 状況                                          | 対処                                               |
| --------------------------------------------- | -------------------------------------------------- |
| planner の計画が曖昧                          | ユーザーに追加質問（implementer に進めない）       |
| implementer がテスト失敗                      | 再実装、または計画書の見直し（planner 再呼び出し） |
| simplifier が「これ以上簡素化できない」と判断 | 次の docs-keeper へ進む                            |
| 並列implementerでIF衝突発覚                   | 並列を中断し、planner で IF を再合意               |

## 起動コマンド例

ユーザー指示「エージェントチームで HeightPlane の調整機能を追加して」に対する起動：

1. まず implementation-planner agent を起動して計画策定
2. 計画を確認・承認
3. plan-implementer agent を起動（複数モジュールなら並列）
4. 完了後 code-simplifier agent を起動
5. 必要なら 3 → 4 を繰り返す
6. 最後に docs-keeper agent を起動

各 agent 起動時は `subagent_type` で指定し、必要なコンテキスト（計画書パス、対象ファイル等）を prompt に含める。
